"""
Hybrid Company-Aware Course Recommendation Engine
=================================================
( ... docstring asli dipertahankan ... )
"""
import logging
import time
from typing import Dict, List, Optional, Any, Set, Tuple

from django.core.cache import cache
from django.db import connection, OperationalError

# NEW: optional numpy / sklearn imports (safe fallback)
try:
    import numpy as np
    from sklearn.ensemble import RandomForestClassifier
    _HAVE_SKLEARN = True
except Exception:  # ImportError, etc.
    _HAVE_SKLEARN = False

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# CONFIG
# ------------------------------------------------------------------
USE_RANDOM_FOREST = True            # <-- AKTIFKAN RF
DEBUG_SQL = False
MODEL_TTL_SEC = 24 * 3600           # cache model 24 jam
RECOMMENDATION_TTL_SEC = 6 * 3600   # cache rekomendasi 6 jam
MAX_RETURN_COURSES = 10             # batas rekomendasi dikirim ke UI


# ============================================================
# CACHE (per student + company)
# ============================================================
class ModelCache:
    """
    Cache berbasis Django cache.
    Key berformat:
        ml_<type>_<student_id>_<company_id|none>
    """
    @staticmethod
    def _key(student_id: str, data_type: str, company_id: Optional[str]) -> str:
        return f"ml_{data_type}_{student_id}_{company_id or 'none'}"

    # ---------------- model ----------------
    @staticmethod
    def get_model(student_id: str, company_id: Optional[str] = None):
        data = cache.get(ModelCache._key(student_id, "model", company_id))
        if data and (time.time() - data.get("created_at", 0)) < MODEL_TTL_SEC:
            return data.get("model")
        return None

    @staticmethod
    def set_model(student_id: str, company_id: Optional[str], model_data: dict):
        cache.set(
            ModelCache._key(student_id, "model", company_id),
            {"model": model_data, "created_at": time.time()},
            MODEL_TTL_SEC,
        )

    # ------------- recommendations ----------
    @staticmethod
    def get_recommendations(student_id: str, company_id: Optional[str] = None):
        data = cache.get(ModelCache._key(student_id, "recommendations", company_id))
        if data and (time.time() - data.get("created_at", 0)) < RECOMMENDATION_TTL_SEC:
            return data.get("recommendations")
        return None

    @staticmethod
    def set_recommendations(student_id: str, company_id: Optional[str], recs: list):
        cache.set(
            ModelCache._key(student_id, "recommendations", company_id),
            {"recommendations": recs, "created_at": time.time()},
            RECOMMENDATION_TTL_SEC,
        )

    # ------------- invalidate ---------------
    @staticmethod
    def invalidate(student_id: str, company_id: Optional[str] = None):
        cache.delete(ModelCache._key(student_id, "model", company_id))
        cache.delete(ModelCache._key(student_id, "recommendations", company_id))
        logger.info("[CACHE] Invalidated student=%s company=%s", student_id, company_id or "none")

    @staticmethod
    def invalidate_all(student_id: str):
        """
        Hapus seluruh cache student (semua company).
        Implementasi minimal: hapus 'none' (default) & scan prefix di in‑memory cache.
        Untuk backend Redis kamu dapat override dgn pattern delete.
        """
        # hapus key 'none'
        ModelCache.invalidate(student_id, None)
        # brute-force: jika kamu punya daftar company_id di DB, hapus juga
        try:
            with connection.cursor() as cur:
                cur.execute("""
                    SELECT DISTINCT company_id
                    FROM public.student_company_choice
                    WHERE student_id = %s
                """, [student_id])
                for (cid,) in cur.fetchall():
                    ModelCache.invalidate(student_id, cid)
        except Exception as e:
            logger.warning("[CACHE] invalidate_all scan failed: %s", e)


# ============================================================
# ENGINE
# ============================================================
class CourseRecommendationEngine:
    # -----------------------------
    # PUBLIC ENTRY
    # -----------------------------
    def get_recommendations(
        self,
        student_id: str,
        company_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Entry utama: kembalikan daftar course rekomendasi.
        Jika company_id None -> coba ambil company terbaru yg dipilih student.
        """
        if company_id is None:
            company_id = self._get_student_selected_company(student_id)

        cached = ModelCache.get_recommendations(student_id, company_id)
        if cached:
            logger.debug("[CACHE] Using recommendations cache student=%s company=%s", student_id, company_id)
            return cached

        # Data student
        student_data = self._get_student_data(student_id)
        if not student_data:
            recs = self._get_fallback_courses()
            ModelCache.set_recommendations(student_id, company_id, recs)
            return recs

        # Data company (required skills)
        company_skills = self._get_company_required_skills(company_id) if company_id else []
        # Skill dari sertifikat student
        cert_skills = self._get_student_certificate_skills(student_id)

        # Build model (cache)
        model_data = self._get_or_train_model(student_id, company_id, student_data, company_skills, cert_skills)

        # Ambil course yg belum diambil student + skill_map
        enrolled_ids = list(student_data['enrollments'].keys())
        courses = self._get_available_courses_with_skillmap(enrolled_ids)
        if not courses:
            recs = self._get_fallback_courses()
            ModelCache.set_recommendations(student_id, company_id, recs)
            return recs

        # Skoring
        if USE_RANDOM_FOREST and model_data.get("rf_model"):
            recs = self._score_courses_rf(courses, student_data, company_skills, cert_skills, model_data)
        else:
            recs = self._score_courses_gap_based(courses, student_data, company_skills, cert_skills, model_data)

        # Dedup sebelum cache
        recs = _dedupe_recommendations(recs)[:MAX_RETURN_COURSES]

        ModelCache.set_recommendations(student_id, company_id, recs)
        return recs

    # ========================================================
    # STUDENT DATA
    # ========================================================
    def _get_student_data(self, student_id: str) -> Optional[Dict[str, Any]]:
        """
        Ambil skill student + enrollment.
        """
        data = {
            "student_id": student_id,
            "skills": {"hard": [], "soft": []},
            "enrollments": {},
            "preferred_categories": [],
        }
        try:
            with connection.cursor() as cur:
                # Skills
                cur.execute(
                    """
                    SELECT hard_skill, soft_skill
                    FROM public.studentskill
                    WHERE student_id = %s
                    """,
                    [student_id],
                )
                row = cur.fetchone()
                if row:
                    if row[0]:
                        data["skills"]["hard"] = [s.strip() for s in row[0].split(",") if s.strip()]
                    if row[1]:
                        data["skills"]["soft"] = [s.strip() for s in row[1].split(",") if s.strip()]

                # Enrollments
                cur.execute(
                    """
                    SELECT e.course_id, c.subject, c.concentration, e.grade
                    FROM public.enrollment e
                    JOIN public.course c ON e.course_id = c.course_id
                    WHERE e.student_id::text = %s
                    """,
                    [student_id],
                )
                for cid, subject, conc, grade in cur.fetchall():
                    progress = 100 if grade else 0
                    data["enrollments"][cid] = {
                        "name": subject,
                        "category": conc,
                        "progress": progress,
                        "grade": grade,
                    }
        except OperationalError as e:
            logger.error("DB error (student data): %s", e)
            return None

        if not data["skills"]["hard"] and not data["enrollments"]:
            return None
        return data

    # ========================================================
    # COMPANY DATA
    # ========================================================
    def _get_student_selected_company(self, student_id: str) -> Optional[str]:
        """
        Ambil company_id terakhir yg dipilih student dari student_company_choice.
        """
        try:
            with connection.cursor() as cur:
                cur.execute(
                    """
                    SELECT company_id
                    FROM public.student_company_choice
                    WHERE student_id = %s
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    [student_id],
                )
                row = cur.fetchone()
                return row[0] if row else None
        except Exception as e:
            logger.error("Error fetching student company choice: %s", e)
            return None

    def _get_company_required_skills(self, company_id: str) -> List[Dict[str, str]]:
        """
        Ambil skill requirements perusahaan.
        Return: list dict {skill_name, skill_type}
        """
        if not company_id:
            return []
        try:
            with connection.cursor() as cur:
                cur.execute(
                    """
                    SELECT s.skill_name, s.skill_type
                    FROM public.company_requirement_skill crs
                    JOIN public.skill s ON crs.skill_id = s.skill_id
                    WHERE crs.cr_id = %s
                    """,
                    [company_id],
                )
                rows = cur.fetchall()
            return [{"skill_name": r[0], "skill_type": r[1]} for r in rows]
        except Exception as e:
            logger.error("Error fetching company required skills: %s", e)
            return []

    def _get_student_certificate_skills(self, student_id: str) -> Set[str]:
        """
        Kumpulkan skill dari sertifikat student.
        """
        skills = set()
        try:
            with connection.cursor() as cur:
                cur.execute(
                    """
                    SELECT skill_name
                    FROM public.certificate
                    WHERE student_id = %s
                    """,
                    [student_id],
                )
                skills = {r[0].strip() for r in cur.fetchall() if r[0]}
        except Exception as e:
            logger.error("Error fetching student certificate skills: %s", e)
        return skills

    # ========================================================
    # COURSE DATA (dengan skill_map)
    # ========================================================
    def _get_available_courses_with_skillmap(self, enrolled_ids: List[int]) -> List[Dict[str, Any]]:
        """
        Ambil course + skill_map.hard_skill (skills yg diajarkan course).
        Hanya ambil course yg BELUM di-enroll student (enrolled_ids).
        """
        if enrolled_ids:
            sql = """
                SELECT c.course_id, c.subject, c.concentration, c.curriculum, c.sks, c.type, c.semester,
                       sm.hard_skill
                FROM public.course c
                LEFT JOIN public.skill_map sm ON sm.course_id = c.course_id
                WHERE NOT (c.course_id = ANY(%s::int[]))
                ORDER BY c.semester DESC
                LIMIT 200
            """
            params = [enrolled_ids]
        else:
            sql = """
                SELECT c.course_id, c.subject, c.concentration, c.curriculum, c.sks, c.type, c.semester,
                       sm.hard_skill
                FROM public.course c
                LEFT JOIN public.skill_map sm ON sm.course_id = c.course_id
                ORDER BY c.semester DESC
                LIMIT 200
            """
            params = []

        try:
            with connection.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
        except Exception as e:
            logger.error("Error fetching courses w/ skill_map: %s", e)
            return []

        courses = []
        for r in rows:
            hard_str = r[7] or ""
            hard_list = [s.strip() for s in hard_str.split(",") if s.strip()]
            courses.append(
                {
                    "course_id": r[0],
                    "course_name": r[1],
                    "course_category": r[2] or r[5],
                    "description": f"{r[3]} ({r[5]})",
                    "difficulty_level": self._map_sks_to_difficulty(r[4]),
                    "difficulty_num": self._difficulty_to_num(self._map_sks_to_difficulty(r[4])),
                    "teach_skills": hard_list,
                }
            )
        return courses

    # ========================================================
    # FALLBACK
    # ========================================================
    def _get_fallback_courses(self) -> List[Dict[str, Any]]:
        """
        fallback course terbaru (lama).
        """
        try:
            with connection.cursor() as cur:
                cur.execute(
                    """
                    SELECT course_id, subject, concentration, curriculum, sks, type
                    FROM public.course
                    ORDER BY semester DESC
                    LIMIT 5
                    """
                )
                rows = cur.fetchall()
            return [
                {
                    "course_id": r[0],
                    "course_name": r[1],
                    "course_category": r[2] or r[5],
                    "description": f"{r[3]} ({r[5]})",
                    "difficulty_level": self._map_sks_to_difficulty(r[4]),
                    "score": 0.5,
                    "priority": "Medium",
                    "reasons": ["Popular / newest course."],
                    "covers_skills": [],
                    "reinforces_skills": [],
                }
                for r in rows
            ]
        except Exception as e:
            logger.error("Error fetching fallback courses: %s", e)
            return []

    # ========================================================
    # MODEL
    # ========================================================
    def _get_or_train_model(
        self,
        student_id: str,
        company_id: Optional[str],
        student_data: Dict[str, Any],
        company_skills: List[Dict[str, str]],
        cert_skills: Set[str],
    ):
        model = ModelCache.get_model(student_id, company_id)
        if model:
            return model

        if USE_RANDOM_FOREST and _HAVE_SKLEARN:
            model = self._train_random_forest(student_data, company_skills, cert_skills)
            # Jika training gagal / kosong -> fallback rule
            if not model.get("rf_model"):
                logger.warning("[RF] Training unavailable; fallback rule-based.")
                model = self._train_rule_based_model(student_data, company_skills, cert_skills)
        else:
            model = self._train_rule_based_model(student_data, company_skills, cert_skills)

        ModelCache.set_model(student_id, company_id, model)
        return model

    def _train_rule_based_model(
        self,
        student_data: Dict[str, Any],
        company_skills: List[Dict[str, str]],
        cert_skills: Set[str],
    ):
        return {
            "type": "gap_rule",
            "completion_rate": self._calc_completion_rate(student_data),
            "company_skill_count": len(company_skills),
            "cert_skill_count": len(cert_skills),
        }

    # ------------------ RF TRAINING (NEW) -------------------
    def _train_random_forest(
        self,
        student_data: Dict[str, Any],
        company_skills: List[Dict[str, str]],
        cert_skills: Set[str],
    ):
        """
        Latih RandomForestClassifier GLOBAL dari histori enrollment semua student.
        Label: 1 jika grade tidak NULL & bukan kosong & bukan 'F' (bisa kamu refine).
        """
        if not _HAVE_SKLEARN:
            return {"type": "rf", "rf_model": None, "meta": {"reason": "sklearn missing"}}

        # Ambil data training global
        X, y = self._extract_training_rows()

        if X.size == 0 or len(y) < 5:
            logger.warning("[RF] Not enough training rows (%s).", len(y))
            return {"type": "rf", "rf_model": None, "meta": {"rows": len(y)}}

        rf = RandomForestClassifier(
            n_estimators=200,
            max_depth=None,
            random_state=42,
            n_jobs=-1,
            class_weight="balanced",
        )
        rf.fit(X, y)
        logger.info("[RF] Trained model on %s rows.", len(y))
        return {"type": "rf", "rf_model": rf, "meta": {"rows": len(y)}}

    # ---- extract training rows from DB ----
    def _extract_training_rows(self) -> Tuple[np.ndarray, np.ndarray]:
        """
        Bangun dataset global:
            - Loop seluruh enrollment.
            - Feature build pakai snapshot skill student saat ini (approx).
              (Kalau mau akurat, perlu historical snapshot; di sini sederhana.)
            - Label = pass (grade not null & not empty & grade not in ('F','E')).
        """
        rows_feat: List[List[float]] = []
        rows_lbl: List[int] = []

        try:
            with connection.cursor() as cur:
                cur.execute("""
                    SELECT e.student_id::text, e.course_id, e.grade,
                           c.sks, c.type, c.concentration,
                           sm.hard_skill
                    FROM public.enrollment e
                    JOIN public.course c ON e.course_id = c.course_id
                    LEFT JOIN public.skill_map sm ON sm.course_id = c.course_id
                """)
                enroll_rows = cur.fetchall()
        except Exception as e:
            logger.error("[RF] extract_training_rows query failed: %s", e)
            return np.empty((0, 6)), np.array([])

        # Cache mini utk student skills & cert
        stud_cache: Dict[str, Dict[str, Any]] = {}

        for s_id, c_id, grade, sks, ctype, conc, hard_skill in enroll_rows:
            if s_id not in stud_cache:
                stud_cache[s_id] = {
                    "skills": self._get_student_skills_simple(s_id),
                    "certs": self._get_student_certificate_skills(s_id),
                    "company_skills": self._get_company_required_skills(self._get_student_selected_company(s_id)),
                    "completion_rate": self._calc_completion_rate(self._get_student_data(s_id) or {"enrollments": {}}),
                }
            snap = stud_cache[s_id]
            teach_skills = [s.strip() for s in (hard_skill or "").split(",") if s.strip()]
            feat = self._build_features(
                teach_skills=teach_skills,
                student_skills=snap["skills"],
                cert_skills=snap["certs"],
                company_skills=snap["company_skills"],
                completion_rate=snap["completion_rate"],
                sks=sks,
            )
            rows_feat.append(feat)
            lbl = self._grade_to_label(grade)
            rows_lbl.append(lbl)

        if not rows_feat:
            return np.empty((0, 6)), np.array([])

        return np.array(rows_feat, dtype=float), np.array(rows_lbl, dtype=int)

    # helper: get student skills (dict hard/soft lists)
    def _get_student_skills_simple(self, student_id: str) -> Dict[str, List[str]]:
        out = {"hard": [], "soft": []}
        try:
            with connection.cursor() as cur:
                cur.execute("""
                    SELECT hard_skill, soft_skill
                    FROM public.studentskill
                    WHERE student_id = %s
                """, [student_id])
                row = cur.fetchone()
                if row:
                    if row[0]:
                        out["hard"] = [s.strip() for s in row[0].split(",") if s.strip()]
                    if row[1]:
                        out["soft"] = [s.strip() for s in row[1].split(",") if s.strip()]
        except Exception:
            pass
        return out

    # helper: encode grade
    def _grade_to_label(self, grade: Optional[str]) -> int:
        if not grade:
            return 0
        g = str(grade).strip().upper()
        if g in ("A", "A-", "B+", "B", "B-", "C+", "C", "PASS", "P"):
            return 1
        return 0

    # build features (SAME used by training & scoring)
    def _build_features(
        self,
        teach_skills: List[str],
        student_skills: Dict[str, List[str]],
        cert_skills: Set[str],
        company_skills: List[Dict[str, str]],
        completion_rate: float,
        sks: Optional[int],
    ) -> List[float]:
        stu_all = {s.lower() for s in (student_skills.get("hard", []) + student_skills.get("soft", []))}
        cert_lower = {s.lower() for s in cert_skills}
        teach_lower = {s.lower() for s in teach_skills if s}
        req_lower = {d["skill_name"].lower() for d in company_skills} if company_skills else set()

        covered_gap = teach_lower & (req_lower - stu_all)
        reinforce = teach_lower & stu_all & cert_lower

        feat = [
            float(len(covered_gap)),                       # f0
            float(len(reinforce)),                         # f1
            float(completion_rate),                        # f2
            float(self._difficulty_to_num(self._map_sks_to_difficulty(sks))),  # f3
            float(len(stu_all)),                           # f4
            float(len(req_lower)),                         # f5
        ]
        return feat

    def _difficulty_to_num(self, diff: str) -> int:
        dl = (diff or "").lower()
        if dl.startswith("beg"): return 1
        if dl.startswith("int"): return 2
        if dl.startswith("adv"): return 3
        return 1

    # ========================================================
    # SCORING: GAP-BASED (existing)
    # ========================================================
    def _score_courses_gap_based(
        self,
        courses: List[Dict[str, Any]],
        student_data: Dict[str, Any],
        company_skills: List[Dict[str, str]],
        cert_skills: Set[str],
        model_data: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        student_hard = {s.lower() for s in student_data["skills"]["hard"]}
        student_soft = {s.lower() for s in student_data["skills"]["soft"]}
        student_all = student_hard | student_soft

        cert_lower = {s.lower() for s in cert_skills}
        req_lower = {d["skill_name"].lower() for d in company_skills} if company_skills else set()
        gap_set = req_lower - student_all

        recs: List[Dict[str, Any]] = []
        for c in courses:
            teach = {s.lower() for s in c.get("teach_skills", []) if s}
            if not teach:
                continue

            covered_gap = teach & gap_set
            reinforce = teach & student_all & cert_lower

            score = 0.0
            score += len(covered_gap) * 1.0
            score += len(reinforce) * 1.5
            score += model_data.get("completion_rate", 0) * 0.2

            if score <= 0:
                continue

            recs.append(
                {
                    "course_id": c["course_id"],
                    "course_name": c["course_name"],
                    "course_category": c["course_category"],
                    "description": c["description"],
                    "difficulty_level": c["difficulty_level"],
                    "score": round(score, 2),
                    "priority": self._get_priority_from_raw(score),
                    "reasons": self._build_gap_reasons(covered_gap, reinforce),
                    "covers_skills": sorted(list(covered_gap)),
                    "reinforces_skills": sorted(list(reinforce)),
                }
            )

        if not recs:
            return self._get_fallback_courses()

        recs.sort(key=lambda x: x["score"], reverse=True)
        return recs

    # ========================================================
    # SCORING: RANDOM FOREST (NEW)
    # ========================================================
    def _score_courses_rf(
        self,
        courses: List[Dict[str, Any]],
        student_data: Dict[str, Any],
        company_skills: List[Dict[str, str]],
        cert_skills: Set[str],
        model_data: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """
        Gunakan rf_model.predict_proba → skor → map ke skala 0..3 agar
        priority mapping konsisten (High>=3, Medium>=1.5, Low<1.5).
        """
        rf = model_data.get("rf_model")
        if rf is None:
            # fallback
            return self._score_courses_gap_based(courses, student_data, company_skills, cert_skills, model_data)

        student_skills = student_data["skills"]
        completion_rate = self._calc_completion_rate(student_data)

        feats = []
        meta = []
        for c in courses:
            feat = self._build_features(
                teach_skills=c.get("teach_skills", []),
                student_skills=student_skills,
                cert_skills=cert_skills,
                company_skills=company_skills,
                completion_rate=completion_rate,
                sks=self._difficulty_from_label(c["difficulty_level"]),
            )
            feats.append(feat)
            meta.append(c)

        X = np.array(feats, dtype=float)
        try:
            proba = rf.predict_proba(X)[:, 1]  # prob kelas 1
        except Exception as e:
            logger.error("[RF] predict_proba failed: %s", e)
            return self._score_courses_gap_based(courses, student_data, company_skills, cert_skills, model_data)

        # skala → 0..3
        scores = proba * 3.0

        # recompute coverage for reasons (pakai logika rule ringan)
        stu_all = {s.lower() for s in (student_skills["hard"] + student_skills["soft"])}
        cert_lower = {s.lower() for s in cert_skills}
        req_lower = {d["skill_name"].lower() for d in company_skills} if company_skills else set()
        gap_set = req_lower - stu_all

        recs = []
        for proba_score, c in zip(scores, meta):
            teach = {s.lower() for s in c.get("teach_skills", []) if s}
            covered_gap = teach & gap_set
            reinforce = teach & stu_all & cert_lower

            recs.append(
                {
                    "course_id": c["course_id"],
                    "course_name": c["course_name"],
                    "course_category": c["course_category"],
                    "description": c["description"],
                    "difficulty_level": c["difficulty_level"],
                    "score": round(float(proba_score), 2),
                    "priority": self._get_priority_from_raw(float(proba_score)),
                    "reasons": self._build_gap_reasons(covered_gap, reinforce),
                    "covers_skills": sorted(list(covered_gap)),
                    "reinforces_skills": sorted(list(reinforce)),
                }
            )

        recs.sort(key=lambda x: x["score"], reverse=True)
        return recs

    def _difficulty_from_label(self, diff_label: str) -> int:
        # helper utk _build_features(sks=?) kita butuh angka; gunakan mapping terbalik
        dl = (diff_label or "").lower()
        if dl.startswith("beg"): return 2  # assume 2 sks
        if dl.startswith("int"): return 3  # 3 sks
        if dl.startswith("adv"): return 5  # 5 sks
        return 2

    # ========================================================
    # UTIL (SCORING SUPPORT)
    # ========================================================
    def _build_gap_reasons(self, covered_gap, reinforce):
        reasons = []
        if covered_gap:
            reasons.append(f"Covers missing skills: {', '.join(sorted(covered_gap))}.")
        if reinforce:
            reasons.append(f"Reinforces certified skills: {', '.join(sorted(reinforce))}.")
        if not reasons:
            reasons.append("Recommended based on your profile.")
        return reasons

    def _get_priority_from_raw(self, raw_score: float) -> str:
        if raw_score >= 3:
            return "High"
        if raw_score >= 1.5:
            return "Medium"
        return "Low"

    # ========================================================
    # LEGACY UTIL
    # ========================================================
    def _calc_completion_rate(self, student_data):
        enrollments = student_data.get("enrollments", {})
        if not enrollments:
            return 0.0
        completed = sum(1 for e in enrollments.values() if e.get("progress", 0) >= 80)
        return completed / len(enrollments)

    def _map_sks_to_difficulty(self, sks):
        try:
            sks = int(sks)
        except Exception:
            return "Beginner"
        if sks <= 2:
            return "Beginner"
        if 3 <= sks <= 4:
            return "Intermediate"
        return "Advanced"


# ============================================================
# DEDUPE (shared w/ views)
# ============================================================
_PRIORITY_RANK = {'low': 0, 'medium': 1, 'high': 2}

def _dedupe_recommendations(recs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not recs:
        return []
    dedup = {}
    for r in recs:
        if not isinstance(r, dict):
            continue
        cid = r.get('course_id')
        if cid is None:
            cid = f"__{r.get('course_name','')}"
        keep = dedup.get(cid)
        if keep is None:
            dedup[cid] = r
        else:
            if r.get('score', 0) > keep.get('score', 0):
                dedup[cid] = r
            elif r.get('score', 0) == keep.get('score', 0):
                kp = _PRIORITY_RANK.get(str(keep.get('priority','')).lower(), 0)
                rp = _PRIORITY_RANK.get(str(r.get('priority','')).lower(), 0)
                if rp > kp:
                    dedup[cid] = r
    out = list(dedup.values())
    out.sort(key=lambda x: x.get('score', 0), reverse=True)
    return out


# ============================================================
# PUBLIC APIs
# ============================================================
def get_course_recommendations(student_id: str, company_id: Optional[str] = None) -> List[Dict[str, Any]]:
    return CourseRecommendationEngine().get_recommendations(student_id, company_id)

def get_skill_gap_for_student_company(student_id: str, company_id: Optional[str] = None) -> List[str]:
    eng = CourseRecommendationEngine()
    if company_id is None:
        company_id = eng._get_student_selected_company(student_id)
    student_data = eng._get_student_data(student_id) or {}
    company_skills = eng._get_company_required_skills(company_id) if company_id else []
    student_all = {
        s.lower()
        for s in (
            student_data.get("skills", {}).get("hard", [])
            + student_data.get("skills", {}).get("soft", [])
        )
    }
    req_lower = {d["skill_name"].lower() for d in company_skills}
    missing = sorted(list(req_lower - student_all))
    return missing

# ============================================================
# WRAPPER FUNCTION FOR REUSABILITY
# ============================================================
def run_course_recommendation(student_id: str) -> List[Dict[str, Any]]:
    """
    Wrapper function untuk menjalankan rekomendasi course berdasarkan student_id.
    Ini memakai logika dari get_course_recommendations agar tetap reusable.
    
    Args:
        student_id (str): ID student yang akan mendapat rekomendasi
        
    Returns:
        List[Dict[str, Any]]: List rekomendasi course dengan format:
            - course_id: ID course
            - course_name: Nama course
            - course_category: Kategori/konsentrasi course
            - description: Deskripsi course
            - difficulty_level: Level kesulitan (Beginner/Intermediate/Advanced)
            - score: Skor rekomendasi (0-3)
            - priority: Prioritas (High/Medium/Low)
            - reasons: List alasan mengapa course direkomendasikan
            - covers_skills: Skills yang akan dipelajari untuk mengisi gap
            - reinforces_skills: Skills yang akan diperkuat
    """
    return get_course_recommendations(student_id)