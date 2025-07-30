import logging
import time
from typing import Dict, List, Optional, Any, Set, Tuple

from django.core.cache import cache
from django.db import connection, OperationalError
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver

# NEW: optional numpy / sklearn imports (safe fallback)
try:
    import numpy as np
    from sklearn.ensemble import RandomForestClassifier
    _HAVE_SKLEARN = True
except Exception:  # ImportError, etc.
    _HAVE_SKLEARN = False

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# CONFIG - ENHANCED
# ------------------------------------------------------------------
USE_RANDOM_FOREST = True            
DEBUG_SQL = False
MODEL_TTL_SEC = 24 * 3600           # cache model 24 jam
RECOMMENDATION_TTL_SEC = 6 * 3600   # cache rekomendasi 6 jam
MAX_RETURN_COURSES = None           # Show all relevant courses
REQUIRE_SKILLS_INPUT = True         # STRICT: Require skills input
REQUIRE_INTERNSHIP_SELECTION = True # STRICT: Require internship selection
MIN_SKILLS_REQUIRED = 3             # INCREASED: Minimum 3 skills (sesuai views)
MIN_SCORE_THRESHOLD = 0.5           # NEW: Minimum score untuk recommendations

# ============================================================
# ENHANCED CACHE dengan Auto-Invalidation
# ============================================================
class ModelCache:
    """
    Enhanced cache dengan auto-invalidation support
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

    # ------------- profile status ----------
    @staticmethod
    def get_profile_status(student_id: str):
        data = cache.get(f"profile_status_{student_id}")
        if data and (time.time() - data.get("created_at", 0)) < 3600:  # 1 hour cache
            return data.get("status")
        return None

    @staticmethod
    def set_profile_status(student_id: str, status: dict):
        cache.set(
            f"profile_status_{student_id}",
            {"status": status, "created_at": time.time()},
            3600,
        )

    # ------------- invalidate (ENHANCED) ---------------
    @staticmethod
    def invalidate(student_id: str, company_id: Optional[str] = None):
        """Invalidate specific student-company cache"""
        cache.delete(ModelCache._key(student_id, "model", company_id))
        cache.delete(ModelCache._key(student_id, "recommendations", company_id))
        cache.delete(f"profile_status_{student_id}")
        logger.info("[CACHE] Invalidated student=%s company=%s", student_id, company_id or "none")

    @staticmethod
    def invalidate_all(student_id: str):
        """Invalidate ALL cache for student (all companies)"""
        # Hapus profile status
        cache.delete(f"profile_status_{student_id}")
        
        # Hapus default (none company)
        ModelCache.invalidate(student_id, None)
        
        # Hapus semua company-specific cache
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

    @staticmethod
    def invalidate_on_student_update(student_id: str, update_type: str = "general"):
        """
        NEW: Auto-invalidate cache saat student update data
        """
        ModelCache.invalidate_all(student_id)
        logger.info("[CACHE] Auto-invalidated for student=%s due to %s update", student_id, update_type)


# ============================================================
# ENHANCED RECOMMENDATION ENGINE - STRICT VALIDATION
# ============================================================
class EnhancedCourseRecommendationEngine:
    """
    Enhanced version dengan STRICT validation dan targeted recommendations
    HANYA menampilkan course yang relevan dengan internship + skills
    """
    
    def get_recommendations(
        self,
        student_id: str,
        company_id: Optional[str] = None,
        bypass_validation: bool = False,
    ) -> Dict[str, Any]:
        """
        STRICT: Entry utama dengan validation - NO recommendations without complete profile
        """
        # Initialize result structure
        result = {
            "has_internship": False,
            "has_skills": False,
            "recommendations": [],  # EMPTY by default
            "skill_gap": [],
            "company_name": "",
            "message": "",
            "profile_completion": {
                "skills_completed": False,
                "internship_selected": False,
                "skills_count": 0,
                "missing_requirements": [],
                "completion_percentage": 0
            },
            "metadata": {
                "timestamp": time.time(),
                "cache_used": False,
                "validation_bypassed": bypass_validation,
                "total_courses_evaluated": 0,
                "courses_above_threshold": 0
            }
        }

        # 1. STRICT SKILLS VALIDATION
        profile_status = self._get_comprehensive_profile_status(student_id)
        has_skills = profile_status["has_skills"]
        skills_count = profile_status["skills_count"]
        
        # 2. STRICT INTERNSHIP VALIDATION
        if company_id is None:
            company_id = self._get_student_selected_company(student_id)
        has_internship = bool(company_id)
        
        # 3. UPDATE RESULT STATUS
        result.update({
            "has_skills": has_skills,
            "has_internship": has_internship,
            "company_name": self._get_company_name(company_id) if company_id else "",
            "profile_completion": {
                "skills_completed": has_skills,
                "internship_selected": has_internship,
                "skills_count": skills_count,
                "missing_requirements": self._get_missing_requirements(has_skills, has_internship),
                "completion_percentage": self._calculate_completion_percentage(has_skills, has_internship)
            }
        })

        # 4. ABSOLUTE GATE - NO DATA = NO RECOMMENDATIONS (TIDAK ADA BYPASS)
        if not has_skills or not has_internship:
            result["message"] = self._build_completion_message(has_skills, has_internship, skills_count)
            result["recommendations"] = []  # EXPLICITLY EMPTY
            logger.info(
                "[RECOMMENDATION] STRICT validation failed for student=%s (skills=%s, internship=%s)", 
                student_id, has_skills, has_internship
            )
            return result

        # 5. CHECK CACHE (only after validation passes)
        cached = ModelCache.get_recommendations(student_id, company_id)
        if cached:
            result["recommendations"] = cached
            result["skill_gap"] = self._get_skill_gap_for_student_company(student_id, company_id)
            result["metadata"]["cache_used"] = True
            logger.debug("[CACHE] Using recommendations cache student=%s company=%s", student_id, company_id)
            return result

        # 6. GENERATE TARGETED RECOMMENDATIONS
        try:
            recommendations = self._generate_targeted_recommendations(student_id, company_id)
            
            # FILTER by minimum score threshold
            filtered_recommendations = [
                r for r in recommendations 
                if r.get('score', 0) >= MIN_SCORE_THRESHOLD
            ]
            
            result["recommendations"] = filtered_recommendations
            result["skill_gap"] = self._get_skill_gap_for_student_company(student_id, company_id)
            result["metadata"]["total_courses_evaluated"] = len(recommendations)
            result["metadata"]["courses_above_threshold"] = len(filtered_recommendations)
            
            # Cache the results
            ModelCache.set_recommendations(student_id, company_id, filtered_recommendations)
            
            logger.info(
                "[RECOMMENDATION] Generated %d recommendations (%d above threshold) for student=%s company=%s",
                len(recommendations), len(filtered_recommendations), student_id, company_id
            )
            
        except Exception as e:
            logger.error("[RECOMMENDATION] Generation failed for student=%s: %s", student_id, str(e))
            result["recommendations"] = []  # NO fallback courses
            result["message"] = "Unable to generate recommendations. Please check your profile completeness."

        return result

    def _get_comprehensive_profile_status(self, student_id: str) -> Dict[str, Any]:
        """
        ENHANCED: Get comprehensive profile completion status with STRICT validation
        """
        cached_status = ModelCache.get_profile_status(student_id)
        if cached_status:
            return cached_status

        status = {
            "has_skills": False,
            "skills_count": 0,
            "hard_skills": [],
            "soft_skills": [],
            "has_enrollments": False,
            "enrollment_count": 0
        }

        try:
            with connection.cursor() as cur:
                # Check skills dengan STRICT validation
                cur.execute("""
                    SELECT hard_skill, soft_skill
                    FROM public.studentskill
                    WHERE student_id = %s
                """, [student_id])
                
                row = cur.fetchone()
                if row:
                    hard_skills = []
                    soft_skills = []
                    
                    # Parse hard skills
                    if row[0] and row[0].strip():
                        hard_skills = [s.strip() for s in row[0].split(",") if s.strip()]
                    
                    # Parse soft skills  
                    if row[1] and row[1].strip():
                        soft_skills = [s.strip() for s in row[1].split(",") if s.strip()]
                    
                    total_skills = len(hard_skills) + len(soft_skills)
                    
                    status.update({
                        "hard_skills": hard_skills,
                        "soft_skills": soft_skills,
                        "skills_count": total_skills,
                        "has_skills": total_skills >= MIN_SKILLS_REQUIRED  # STRICT: minimum 3 skills
                    })

                # Check enrollments
                cur.execute("""
                    SELECT COUNT(*)
                    FROM public.enrollment
                    WHERE student_id::text = %s
                """, [student_id])
                
                enrollment_count = cur.fetchone()[0] or 0
                status.update({
                    "enrollment_count": enrollment_count,
                    "has_enrollments": enrollment_count > 0
                })

        except Exception as e:
            logger.error("Error getting profile status for student %s: %s", student_id, e)

        # Cache the status
        ModelCache.set_profile_status(student_id, status)
        return status

    def _generate_targeted_recommendations(self, student_id: str, company_id: str) -> List[Dict[str, Any]]:
        """
        TARGETED: Generate recommendations yang HANYA fokus pada internship requirements
        """
        # Get student data
        student_data = self._get_student_data(student_id)
        if not student_data:
            logger.warning("[RECOMMENDATION] No student data found for %s", student_id)
            return []

        # Get company requirements - ESSENTIAL
        company_skills = self._get_company_required_skills(company_id)
        if not company_skills:
            logger.warning("[RECOMMENDATION] No company skills found for company %s", company_id)
            return []

        cert_skills = self._get_student_certificate_skills(student_id)

        # Build/get model
        model_data = self._get_or_train_model(student_id, company_id, student_data, company_skills, cert_skills)

        # Get ONLY courses yang dapat membantu dengan company requirements
        enrolled_ids = list(student_data['enrollments'].keys())
        courses = self._get_targeted_courses_for_company_skills(enrolled_ids, company_skills)
        
        if not courses:
            logger.info("[RECOMMENDATION] No relevant courses found for company requirements")
            return []

        # Generate targeted recommendations
        if USE_RANDOM_FOREST and model_data.get("rf_model"):
            recs = self._score_courses_rf_targeted(courses, student_data, company_skills, cert_skills, model_data)
        else:
            recs = self._score_courses_gap_based_targeted(courses, student_data, company_skills, cert_skills, model_data)

        # ENHANCED DEDUPLICATION dengan skill merging
        recs = self._dedupe_recommendations_with_skill_merge(recs)
        
        # FINAL FILTER: Hanya course yang benar-benar membantu skill gap
        filtered_recs = self._filter_truly_relevant_courses(recs, student_data, company_skills)
        
        logger.info(
            "[RECOMMENDATION] Generated %d courses, filtered to %d relevant courses for student=%s company=%s",
            len(recs), len(filtered_recs), student_id, company_id
        )
        
        return filtered_recs

    def _get_targeted_courses_for_company_skills(self, enrolled_ids: List[int], company_skills: List[Dict[str, str]]) -> List[Dict[str, Any]]:
        """
        NEW: Get ONLY courses that can help with company skill requirements
        """
        if not company_skills:
            return []
        
        # Extract required skill names
        required_skill_names = {skill["skill_name"].lower().strip() for skill in company_skills}
        
        try:
            # Build SQL to find courses that teach required skills
            if enrolled_ids:
                sql = """
                    SELECT DISTINCT c.course_id, c.subject, c.concentration, c.curriculum, c.sks, c.type, c.semester,
                           sm.hard_skill
                    FROM public.course c
                    LEFT JOIN public.skill_map sm ON sm.course_id = c.course_id
                    WHERE NOT (c.course_id = ANY(%s::int[]))
                      AND sm.hard_skill IS NOT NULL
                      AND sm.hard_skill != ''
                    ORDER BY c.semester DESC
                """
                params = [enrolled_ids]
            else:
                sql = """
                    SELECT DISTINCT c.course_id, c.subject, c.concentration, c.curriculum, c.sks, c.type, c.semester,
                           sm.hard_skill
                    FROM public.course c
                    LEFT JOIN public.skill_map sm ON sm.course_id = c.course_id
                    WHERE sm.hard_skill IS NOT NULL
                      AND sm.hard_skill != ''
                    ORDER BY c.semester DESC
                """
                params = []

            with connection.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
        except Exception as e:
            logger.error("Error fetching targeted courses: %s", e)
            return []

        relevant_courses = []
        for r in rows:
            hard_str = r[7] or ""
            hard_list = [s.strip() for s in hard_str.split(",") if s.strip()]
            
            # Check if this course teaches any required skills
            course_skills_lower = {skill.lower() for skill in hard_list}
            
            # TARGETED FILTER: Only include if course teaches required skills
            skill_overlap = course_skills_lower.intersection(required_skill_names)
            
            if skill_overlap:  # Course teaches at least one required skill
                relevant_courses.append({
                    "course_id": r[0],
                    "course_name": r[1],
                    "course_category": r[2] or r[5],
                    "description": f"{r[3]} ({r[5]})",
                    "difficulty_level": self._map_sks_to_difficulty(r[4]),
                    "difficulty_num": self._difficulty_to_num(self._map_sks_to_difficulty(r[4])),
                    "teach_skills": hard_list,
                    "relevant_skills": list(skill_overlap),  # Skills that match company requirements
                })
        
        logger.info(
            "[COURSES] Found %d courses (from %d total) that teach required company skills",
            len(relevant_courses), len(rows)
        )
        
        return relevant_courses

    def _filter_truly_relevant_courses(self, recs: List[Dict[str, Any]], student_data: Dict[str, Any], 
                                     company_skills: List[Dict[str, str]]) -> List[Dict[str, Any]]:
        """
        FINAL FILTER: Keep only courses that truly help with skill gaps or reinforce important skills
        """
        if not recs or not company_skills:
            return []
        
        student_all_skills = {
            s.lower() for s in (
                student_data.get("skills", {}).get("hard", []) +
                student_data.get("skills", {}).get("soft", [])
            )
        }
        
        required_skills = {skill["skill_name"].lower() for skill in company_skills}
        skill_gaps = required_skills - student_all_skills
        
        filtered_courses = []
        
        for rec in recs:
            covers_skills = set(skill.lower() for skill in rec.get('covers_skills', []))
            reinforces_skills = set(skill.lower() for skill in rec.get('reinforces_skills', []))
            
            # Keep course if it:
            # 1. Covers at least one skill gap, OR
            # 2. Reinforces important skills AND has good score, OR  
            # 3. Has very high score (indicates strong relevance)
            
            covers_gap = bool(covers_skills.intersection(skill_gaps))
            reinforces_important = bool(reinforces_skills.intersection(required_skills))
            high_score = rec.get('score', 0) >= 2.0
            
            if covers_gap or (reinforces_important and rec.get('score', 0) >= 1.0) or high_score:
                # Add gap analysis to metadata
                rec['metadata'] = rec.get('metadata', {})
                rec['metadata'].update({
                    'covers_skill_gaps': covers_gap,
                    'reinforces_required_skills': reinforces_important,
                    'high_relevance_score': high_score,
                    'gaps_covered': list(covers_skills.intersection(skill_gaps)),
                    'important_skills_reinforced': list(reinforces_skills.intersection(required_skills))
                })
                
                filtered_courses.append(rec)
        
        # Sort by relevance: gap-covering courses first, then by score
        filtered_courses.sort(key=lambda x: (
            -len(x['metadata'].get('gaps_covered', [])),  # Gap coverage first
            -x.get('score', 0)  # Then by score
        ))
        
        return filtered_courses

    def _score_courses_gap_based_targeted(self, courses: List[Dict[str, Any]], student_data: Dict[str, Any],
                                        company_skills: List[Dict[str, str]], cert_skills: Set[str], 
                                        model_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        TARGETED: Score courses dengan fokus pada company skill requirements
        """
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

            # TARGETED SCORING: Heavy weight on gap coverage
            covered_gap = teach & gap_set
            reinforce = teach & student_all & cert_lower
            
            # Additional: skills that support company requirements (even if not gaps)
            supports_company = teach & req_lower

            score = 0.0
            # HIGH weight for covering skill gaps
            score += len(covered_gap) * 2.0  # Increased from 1.0
            # MEDIUM weight for reinforcing certified skills that company needs
            score += len(reinforce & req_lower) * 1.5  # Only certified skills that company needs
            # LOW weight for general reinforcement
            score += len(reinforce - req_lower) * 0.5  # Other reinforcement is less important
            # MEDIUM weight for supporting company skills (not gaps, not reinforcement)
            score += len(supports_company - covered_gap - reinforce) * 1.0
            # Completion rate bonus (reduced importance)
            score += model_data.get("completion_rate", 0) * 0.1  # Reduced from 0.2

            # TARGETED FILTER: Only include courses with meaningful company relevance
            company_relevance = len(covered_gap) + len(supports_company)
            if company_relevance == 0:
                continue  # Skip courses with no company relevance

            if score <= MIN_SCORE_THRESHOLD:
                continue

            # Build detailed reasons
            reasons = []
            if covered_gap:
                gaps_list = sorted(list(covered_gap))
                reasons.append(f"Fills critical skill gaps: {', '.join(gaps_list)}")
            
            if reinforce & req_lower:
                reinforce_list = sorted(list(reinforce & req_lower))
                reasons.append(f"Strengthens company-required skills: {', '.join(reinforce_list)}")
            
            if supports_company - covered_gap - reinforce:
                support_list = sorted(list(supports_company - covered_gap - reinforce))
                reasons.append(f"Develops additional company skills: {', '.join(support_list)}")
            
            if not reasons:
                reasons.append("Supports your internship preparation")

            recs.append({
                "course_id": c["course_id"],
                "course_name": c["course_name"],
                "course_category": c["course_category"],
                "description": c["description"],
                "difficulty_level": c["difficulty_level"],
                "score": round(score, 2),
                "priority": self._get_priority_from_score_targeted(score, len(covered_gap)),
                "reasons": reasons,
                "covers_skills": sorted(list(covered_gap)),
                "reinforces_skills": sorted(list(reinforce)),
                "supports_company_skills": sorted(list(supports_company)),
                "teach_skills": c.get("teach_skills", []),
                "company_relevance_score": company_relevance,
            })

        return recs

    def _get_priority_from_score_targeted(self, score: float, gap_coverage_count: int) -> str:
        """
        TARGETED: Priority based on score AND gap coverage
        """
        # High priority: Good score AND covers gaps
        if score >= 3.0 and gap_coverage_count > 0:
            return "High"
        # High priority: Excellent score regardless
        elif score >= 4.0:
            return "High"
        # Medium priority: Good score OR covers gaps
        elif score >= 2.0 or gap_coverage_count > 0:
            return "Medium"
        # Low priority: Everything else above threshold
        else:
            return "Low"

    # ========================================================
    # EXISTING METHODS (kept for compatibility)
    # ========================================================
    
    def _get_student_data(self, student_id: str) -> Optional[Dict[str, Any]]:
        """Get comprehensive student data with STRICT validation"""
        data = {
            "student_id": student_id,
            "skills": {"hard": [], "soft": []},
            "enrollments": {},
            "preferred_categories": [],
        }
        try:
            with connection.cursor() as cur:
                # Skills with validation
                cur.execute("""
                    SELECT hard_skill, soft_skill
                    FROM public.studentskill
                    WHERE student_id = %s
                """, [student_id])
                
                row = cur.fetchone()
                if row:
                    hard_skills = []
                    soft_skills = []
                    
                    if row[0] and row[0].strip():
                        hard_skills = [s.strip() for s in row[0].split(",") if s.strip()]
                    if row[1] and row[1].strip():
                        soft_skills = [s.strip() for s in row[1].split(",") if s.strip()]
                    
                    data["skills"]["hard"] = hard_skills
                    data["skills"]["soft"] = soft_skills

                # Enrollments
                cur.execute("""
                    SELECT e.course_id, c.subject, c.concentration, e.grade
                    FROM public.enrollment e
                    JOIN public.course c ON e.course_id = c.course_id
                    WHERE e.student_id::text = %s
                """, [student_id])
                
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

        # STRICT validation
        total_skills = len(data["skills"]["hard"]) + len(data["skills"]["soft"])
        if total_skills < MIN_SKILLS_REQUIRED:
            logger.warning("Student %s has insufficient skills: %d (minimum: %d)", student_id, total_skills, MIN_SKILLS_REQUIRED)
            return None  # Return None for insufficient skills
            
        return data

    def _get_student_selected_company(self, student_id: str) -> Optional[str]:
        """Get latest selected company"""
        try:
            with connection.cursor() as cur:
                cur.execute("""
                    SELECT company_id
                    FROM public.student_company_choice
                    WHERE student_id = %s
                    ORDER BY created_at DESC
                    LIMIT 1
                """, [student_id])
                row = cur.fetchone()
                return row[0] if row else None
        except Exception as e:
            logger.error("Error fetching student company choice: %s", e)
            return None

    def _get_company_name(self, company_id: str) -> str:
        """Get company name"""
        if not company_id:
            return ""
        
        try:
            with connection.cursor() as cur:
                cur.execute("""
                    SELECT company_name 
                    FROM public.company_requirement
                    WHERE cr_id = %s
                """, [company_id])
                row = cur.fetchone()
                return row[0] if row else f"Company ID: {company_id}"
        except Exception as e:
            logger.error("Error fetching company name: %s", e)
            return f"Company ID: {company_id}"

    def _get_company_required_skills(self, company_id: str) -> List[Dict[str, str]]:
        """Get company required skills"""
        if not company_id:
            return []
        try:
            with connection.cursor() as cur:
                cur.execute("""
                    SELECT s.skill_name, s.skill_type
                    FROM public.company_requirement_skill crs
                    JOIN public.skill s ON crs.skill_id = s.skill_id
                    WHERE crs.cr_id = %s
                """, [company_id])
                rows = cur.fetchall()
            return [{"skill_name": r[0], "skill_type": r[1]} for r in rows]
        except Exception as e:
            logger.error("Error fetching company required skills: %s", e)
            return []

    def _get_student_certificate_skills(self, student_id: str) -> Set[str]:
        """Get skills from student certificates"""
        skills = set()
        try:
            with connection.cursor() as cur:
                cur.execute("""
                    SELECT skill_name
                    FROM public.certificate
                    WHERE student_id = %s
                """, [student_id])
                skills = {r[0].strip() for r in cur.fetchall() if r[0]}
        except Exception as e:
            logger.error("Error fetching student certificate skills: %s", e)
        return skills

    def _get_skill_gap_for_student_company(self, student_id: str, company_id: Optional[str] = None) -> List[str]:
        """Get skill gap between student and company requirements"""
        if not company_id:
            return []
            
        student_data = self._get_student_data(student_id)
        if not student_data:
            return []
            
        company_skills = self._get_company_required_skills(company_id)
        
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

    # ========================================================
    # ENHANCED DEDUPLICATION WITH SKILL MERGING
    # ========================================================
    
    def _dedupe_recommendations_with_skill_merge(self, recs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        ENHANCED: Deduplicate courses dan merge semua skill information seperti di gambar
        """
        if not recs:
            return []

        dedup = {}
        
        for r in recs:
            if not isinstance(r, dict):
                continue
                
            cid = r.get('course_id')
            if cid is None:
                continue

            if cid not in dedup:
                # First occurrence - initialize with enhanced structure
                enhanced_rec = r.copy()
                enhanced_rec.update({
                    'covers_skills': list(r.get('covers_skills', [])),
                    'reinforces_skills': list(r.get('reinforces_skills', [])),
                    'supports_company_skills': list(r.get('supports_company_skills', [])),
                    'all_taught_skills': list(r.get('teach_skills', [])),
                    'skill_sources': [r.get('priority', 'Unknown')],
                    'merged_from_duplicates': False,
                    'duplicate_count': 1
                })
                dedup[cid] = enhanced_rec
            else:
                # Merge with existing
                existing = dedup[cid]
                existing['merged_from_duplicates'] = True
                existing['duplicate_count'] += 1
                
                # Keep higher score and priority
                if r.get('score', 0) > existing.get('score', 0):
                    existing['score'] = r['score']
                    existing['priority'] = r['priority']
                
                # Merge all skill sets (avoid duplicates)
                existing['covers_skills'] = list(set(existing['covers_skills'] + r.get('covers_skills', [])))
                existing['reinforces_skills'] = list(set(existing['reinforces_skills'] + r.get('reinforces_skills', [])))
                existing['supports_company_skills'] = list(set(existing['supports_company_skills'] + r.get('supports_company_skills', [])))
                existing['all_taught_skills'] = list(set(existing['all_taught_skills'] + r.get('teach_skills', [])))
                existing['skill_sources'].append(r.get('priority', 'Unknown'))
                
                # Merge reasons (unique only)
                existing_reasons = set(existing.get('reasons', []))
                new_reasons = set(r.get('reasons', []))
                existing['reasons'] = list(existing_reasons.union(new_reasons))

        # Convert to final format with enhanced skill information
        enhanced_recommendations = []
        for cid, course in dedup.items():
            # Sort all skill lists
            covers_skills = sorted(course['covers_skills'])
            reinforces_skills = sorted(course['reinforces_skills'])
            supports_company_skills = sorted(course['supports_company_skills'])
            all_taught_skills = sorted(course['all_taught_skills'])
            
            # Build enhanced course information (sesuai format gambar)
            enhanced_course = {
                "course_id": course["course_id"],
                "course_name": course["course_name"],
                "course_category": course["course_category"],
                "description": course["description"],
                "difficulty_level": course["difficulty_level"],
                "score": round(course["score"], 2),
                "priority": course["priority"],
                "reasons": course["reasons"],
                
                # ENHANCED SKILL INFORMATION (seperti di gambar)
                "covers_skills": covers_skills,  # Skills yang mengisi gap
                "reinforces_skills": reinforces_skills,  # Skills yang diperkuat
                "supports_company_skills": supports_company_skills,  # All company-related skills
                "all_taught_skills": all_taught_skills,  # Semua skills yang diajarkan
                "teach_skills": all_taught_skills,  # Backward compatibility
                
                # NEW: Comprehensive skill analysis (untuk UI detail)
                "skill_analysis": {
                    "total_skills_taught": len(all_taught_skills),
                    "gap_filling_skills": len(covers_skills),
                    "reinforcement_skills": len(reinforces_skills),
                    "company_relevant_skills": len(supports_company_skills),
                    "additional_skills": len(set(all_taught_skills) - set(supports_company_skills)),
                    "skill_categories": self._categorize_skills(all_taught_skills),
                },
                
                # NEW: Learning outcomes (untuk display)
                "learning_outcomes": self._build_learning_outcomes(covers_skills, reinforces_skills, all_taught_skills),
                
                # NEW: Skill summary untuk UI (seperti di gambar)
                "skill_summary": self._build_skill_summary(covers_skills, reinforces_skills, supports_company_skills),
                
                # Metadata
                "metadata": {
                    "merged_from_duplicates": course.get('merged_from_duplicates', False),
                    "duplicate_count": course.get('duplicate_count', 1),
                    "skill_sources": list(set(course.get('skill_sources', []))),
                    "recommendation_confidence": self._calculate_recommendation_confidence(course),
                    "company_relevance_score": course.get('company_relevance_score', 0)
                }
            }
            
            enhanced_recommendations.append(enhanced_course)

        # Sort by priority and score
        priority_order = {'High': 3, 'Medium': 2, 'Low': 1}
        enhanced_recommendations.sort(
            key=lambda x: (priority_order.get(x.get('priority', 'Low'), 1), x.get('score', 0)), 
            reverse=True
        )
        
        return enhanced_recommendations

    def _build_skill_summary(self, covers_skills: List[str], reinforces_skills: List[str], supports_company_skills: List[str]) -> str:
        """
        NEW: Build human-readable skill summary (sesuai format di gambar)
        """
        summary_parts = []
        
        if covers_skills:
            summary_parts.append(f"Learn {len(covers_skills)} new skills needed for your internship")
        
        if reinforces_skills:
            summary_parts.append(f"Strengthen {len(reinforces_skills)} existing skills")
        
        additional_company_skills = set(supports_company_skills) - set(covers_skills) - set(reinforces_skills)
        if additional_company_skills:
            summary_parts.append(f"Develop {len(additional_company_skills)} additional company-relevant skills")
        
        if not summary_parts:
            summary_parts.append(f"Enhance your skills for internship preparation")
        
        return " • ".join(summary_parts)

    def _build_learning_outcomes(self, covers_skills: List[str], reinforces_skills: List[str], all_taught_skills: List[str]) -> List[str]:
        """
        NEW: Build specific learning outcomes
        """
        outcomes = []
        
        if covers_skills:
            if len(covers_skills) <= 3:
                outcomes.append(f"Master {', '.join(covers_skills)} required for your internship")
            else:
                outcomes.append(f"Master {', '.join(covers_skills[:3])} and {len(covers_skills)-3} other skills required for your internship")
        
        if reinforces_skills:
            if len(reinforces_skills) <= 2:
                outcomes.append(f"Deepen expertise in {', '.join(reinforces_skills)}")
            else:
                outcomes.append(f"Deepen expertise in {', '.join(reinforces_skills[:2])} and {len(reinforces_skills)-2} other areas")
        
        other_skills = list(set(all_taught_skills) - set(covers_skills) - set(reinforces_skills))
        if other_skills:
            if len(other_skills) <= 2:
                outcomes.append(f"Develop new competencies in {', '.join(other_skills)}")
            else:
                outcomes.append(f"Develop new competencies in {', '.join(other_skills[:2])} and {len(other_skills)-2} other areas")
        
        if not outcomes:
            outcomes.append("Enhance your overall skill profile for internship success")
        
        return outcomes

    def _categorize_skills(self, skills: List[str]) -> Dict[str, List[str]]:
        """
        NEW: Categorize skills by type
        """
        categories = {
            "technical": [],
            "programming": [],
            "soft_skills": [],
            "domain_specific": [],
            "tools": []
        }
        
        for skill in skills:
            skill_lower = skill.lower()
            
            if any(prog in skill_lower for prog in ['python', 'java', 'javascript', 'sql', 'programming', 'coding', 'html', 'css', 'react', 'node']):
                categories["programming"].append(skill)
            elif any(tech in skill_lower for tech in ['database', 'network', 'system', 'software', 'hardware', 'cloud', 'server', 'api']):
                categories["technical"].append(skill)
            elif any(soft in skill_lower for soft in ['communication', 'leadership', 'teamwork', 'management', 'presentation', 'problem solving']):
                categories["soft_skills"].append(skill)
            elif any(tool in skill_lower for tool in ['excel', 'powerpoint', 'photoshop', 'figma', 'git', 'jira', 'confluence']):
                categories["tools"].append(skill)
            else:
                categories["domain_specific"].append(skill)
        
        # Return only non-empty categories
        return {k: v for k, v in categories.items() if v}

    def _calculate_recommendation_confidence(self, course_data: Dict[str, Any]) -> float:
        """
        NEW: Calculate confidence score untuk recommendation
        """
        score = course_data.get('score', 0)
        priority = course_data.get('priority', 'Low')
        covers_count = len(course_data.get('covers_skills', []))
        company_relevance = course_data.get('company_relevance_score', 0)
        
        confidence = 0.0
        
        # Score contribution (0-0.3)
        confidence += min(score / 4.0, 1.0) * 0.3
        
        # Priority contribution (0-0.3)
        priority_scores = {'High': 1.0, 'Medium': 0.6, 'Low': 0.3}
        confidence += priority_scores.get(priority, 0.3) * 0.3
        
        # Gap coverage contribution (0-0.2)
        gap_factor = min(covers_count / 3.0, 1.0)
        confidence += gap_factor * 0.2
        
        # Company relevance contribution (0-0.2)
        relevance_factor = min(company_relevance / 5.0, 1.0)
        confidence += relevance_factor * 0.2
        
        return round(confidence, 2)

    # ========================================================
    # HELPER METHODS untuk Validation & Status
    # ========================================================
    
    def _get_missing_requirements(self, has_skills: bool, has_internship: bool) -> List[str]:
        """Get list of missing requirements"""
        missing = []
        if not has_skills:
            missing.append(f"Add at least {MIN_SKILLS_REQUIRED} skills to your profile")
        if not has_internship:
            missing.append("Select an internship company and position")
        return missing

    def _calculate_completion_percentage(self, has_skills: bool, has_internship: bool) -> int:
        """Calculate profile completion percentage"""
        return (int(has_skills) + int(has_internship)) * 50

    def _build_completion_message(self, has_skills: bool, has_internship: bool, skills_count: int) -> str:
        """Build user-friendly completion message"""
        if not has_skills and not has_internship:
            return f"Complete your profile to see personalized recommendations. You need to add skills (minimum {MIN_SKILLS_REQUIRED}) and select an internship company."
        elif not has_skills:
            return f"Please add your skills first (minimum {MIN_SKILLS_REQUIRED} skills required). You currently have {skills_count} skills."
        elif not has_internship:
            return "Please select an internship company to see tailored course recommendations."
        else:
            return "Profile complete! Generating recommendations..."

    # ========================================================
    # MACHINE LEARNING METHODS (Enhanced dari kode asli)
    # ========================================================
    
    def _get_or_train_model(self, student_id: str, company_id: Optional[str], student_data: Dict[str, Any], 
                           company_skills: List[Dict[str, str]], cert_skills: Set[str]):
        """Get or train ML model"""
        model = ModelCache.get_model(student_id, company_id)
        if model:
            return model

        if USE_RANDOM_FOREST and _HAVE_SKLEARN:
            model = self._train_random_forest(student_data, company_skills, cert_skills)
            if not model.get("rf_model"):
                logger.warning("[RF] Training unavailable; fallback rule-based.")
                model = self._train_rule_based_model(student_data, company_skills, cert_skills)
        else:
            model = self._train_rule_based_model(student_data, company_skills, cert_skills)

        ModelCache.set_model(student_id, company_id, model)
        return model

    def _train_rule_based_model(self, student_data: Dict[str, Any], company_skills: List[Dict[str, str]], cert_skills: Set[str]):
        """Train rule-based model"""
        return {
            "type": "gap_rule",
            "completion_rate": self._calc_completion_rate(student_data),
            "company_skill_count": len(company_skills),
            "cert_skill_count": len(cert_skills),
        }

    def _train_random_forest(self, student_data: Dict[str, Any], company_skills: List[Dict[str, str]], cert_skills: Set[str]):
        """Train Random Forest model"""
        if not _HAVE_SKLEARN:
            return {"type": "rf", "rf_model": None, "meta": {"reason": "sklearn missing"}}

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

    def _extract_training_rows(self) -> Tuple[np.ndarray, np.ndarray]:
        """Extract training data from enrollment history"""
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

        stud_cache: Dict[str, Dict[str, Any]] = {}

        for s_id, c_id, grade, sks, ctype, conc, hard_skill in enroll_rows:
            if s_id not in stud_cache:
                stud_cache[s_id] = {
                    "skills": self._get_student_skills_simple(s_id),
                    "certs": self._get_student_certificate_skills(s_id),
                    "company_skills": self._get_company_required_skills(self._get_student_selected_company(s_id) or ""),
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

    def _get_student_skills_simple(self, student_id: str) -> Dict[str, List[str]]:
        """Get student skills in simple format"""
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
                    if row[0] and row[0].strip():
                        out["hard"] = [s.strip() for s in row[0].split(",") if s.strip()]
                    if row[1] and row[1].strip():
                        out["soft"] = [s.strip() for s in row[1].split(",") if s.strip()]
        except Exception:
            pass
        return out

    def _grade_to_label(self, grade: Optional[str]) -> int:
        """Convert grade to binary label"""
        if not grade:
            return 0
        g = str(grade).strip().upper()
        if g in ("A", "A-", "B+", "B", "B-", "C+", "C", "PASS", "P"):
            return 1
        return 0

    def _build_features(self, teach_skills: List[str], student_skills: Dict[str, List[str]], 
                       cert_skills: Set[str], company_skills: List[Dict[str, str]], 
                       completion_rate: float, sks: Optional[int]) -> List[float]:
        """Build feature vector for ML model"""
        stu_all = {s.lower() for s in (student_skills.get("hard", []) + student_skills.get("soft", []))}
        cert_lower = {s.lower() for s in cert_skills}
        teach_lower = {s.lower() for s in teach_skills if s}
        req_lower = {d["skill_name"].lower() for d in company_skills} if company_skills else set()

        covered_gap = teach_lower & (req_lower - stu_all)
        reinforce = teach_lower & stu_all & cert_lower

        feat = [
            float(len(covered_gap)),
            float(len(reinforce)),
            float(completion_rate),
            float(self._difficulty_to_num(self._map_sks_to_difficulty(sks))),
            float(len(stu_all)),
            float(len(req_lower)),
        ]
        return feat

    def _score_courses_rf_targeted(self, courses: List[Dict[str, Any]], student_data: Dict[str, Any],
                                 company_skills: List[Dict[str, str]], cert_skills: Set[str], 
                                 model_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Score courses using Random Forest model with targeting"""
        rf = model_data.get("rf_model")
        if rf is None:
            return self._score_courses_gap_based_targeted(courses, student_data, company_skills, cert_skills, model_data)

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
            proba = rf.predict_proba(X)[:, 1]
        except Exception as e:
            logger.error("[RF] predict_proba failed: %s", e)
            return self._score_courses_gap_based_targeted(courses, student_data, company_skills, cert_skills, model_data)

        scores = proba * 3.0  # Scale scores

        # Recompute coverage for reasons (same as gap-based method)
        stu_all = {s.lower() for s in (student_skills["hard"] + student_skills["soft"])}
        cert_lower = {s.lower() for s in cert_skills}
        req_lower = {d["skill_name"].lower() for d in company_skills} if company_skills else set()
        gap_set = req_lower - stu_all

        recs = []
        for proba_score, c in zip(scores, meta):
            teach = {s.lower() for s in c.get("teach_skills", []) if s}
            covered_gap = teach & gap_set
            reinforce = teach & stu_all & cert_lower
            supports_company = teach & req_lower

            # Build reasons
            reasons = []
            if covered_gap:
                gaps_list = sorted(list(covered_gap))
                reasons.append(f"Fills critical skill gaps: {', '.join(gaps_list)}")
            
            if reinforce & req_lower:
                reinforce_list = sorted(list(reinforce & req_lower))
                reasons.append(f"Strengthens company-required skills: {', '.join(reinforce_list)}")
            
            if supports_company - covered_gap - reinforce:
                support_list = sorted(list(supports_company - covered_gap - reinforce))
                reasons.append(f"Develops additional company skills: {', '.join(support_list)}")
            
            if not reasons:
                reasons.append("AI-recommended based on your profile and internship needs")

            recs.append({
                "course_id": c["course_id"],
                "course_name": c["course_name"],
                "course_category": c["course_category"],
                "description": c["description"],
                "difficulty_level": c["difficulty_level"],
                "score": round(float(proba_score), 2),
                "priority": self._get_priority_from_score_targeted(float(proba_score), len(covered_gap)),
                "reasons": reasons,
                "covers_skills": sorted(list(covered_gap)),
                "reinforces_skills": sorted(list(reinforce)),
                "supports_company_skills": sorted(list(supports_company)),
                "teach_skills": c.get("teach_skills", []),
                "company_relevance_score": len(supports_company),
            })

        return recs

    # ========================================================
    # UTILITY METHODS
    # ========================================================
    
    def _calc_completion_rate(self, student_data):
        """Calculate student completion rate"""
        enrollments = student_data.get("enrollments", {})
        if not enrollments:
            return 0.0
        completed = sum(1 for e in enrollments.values() if e.get("progress", 0) >= 80)
        return completed / len(enrollments)

    def _map_sks_to_difficulty(self, sks):
        """Map SKS to difficulty level"""
        try:
            sks = int(sks)
        except Exception:
            return "Beginner"
        if sks <= 2:
            return "Beginner"
        if 3 <= sks <= 4:
            return "Intermediate"
        return "Advanced"

    def _difficulty_to_num(self, diff: str) -> int:
        """Convert difficulty level to number"""
        dl = (diff or "").lower()
        if dl.startswith("beg"): return 1
        if dl.startswith("int"): return 2
        if dl.startswith("adv"): return 3
        return 1

    def _difficulty_from_label(self, diff_label: str) -> int:
        """Convert difficulty label back to SKS estimate"""
        dl = (diff_label or "").lower()
        if dl.startswith("beg"): return 2
        if dl.startswith("int"): return 3
        if dl.startswith("adv"): return 5
        return 2


# ============================================================
# PUBLIC API FUNCTIONS - STRICT VALIDATION
# ============================================================

def get_course_recommendations(student_id: str, company_id: Optional[str] = None, 
                             bypass_validation: bool = False) -> Dict[str, Any]:
    """
    STRICT: Get course recommendations - NO BYPASS allowed for incomplete profiles
    
    Args:
        student_id: Student ID
        company_id: Optional company ID
        bypass_validation: IGNORED - validation is always enforced
    
    Returns:
        Dict containing recommendation data (empty if profile incomplete)
    """
    engine = EnhancedCourseRecommendationEngine()
    # Force strict validation - no bypass allowed
    return engine.get_recommendations(student_id, company_id, bypass_validation=False)

def get_skill_gap_for_student_company(student_id: str, company_id: Optional[str] = None) -> List[str]:
    """Get skill gap between student and company requirements"""
    engine = EnhancedCourseRecommendationEngine()
    return engine._get_skill_gap_for_student_company(student_id, company_id)

def check_student_profile_status(student_id: str) -> Dict[str, Any]:
    """
    Check comprehensive student profile status
    """
    engine = EnhancedCourseRecommendationEngine()
    
    # Get profile status
    profile_status = engine._get_comprehensive_profile_status(student_id)
    
    # Get internship status
    company_id = engine._get_student_selected_company(student_id)
    has_internship = bool(company_id)
    
    return {
        "profile_complete": profile_status["has_skills"] and has_internship,
        "has_skills": profile_status["has_skills"],
        "has_internship": has_internship,
        "skills_count": profile_status["skills_count"],
        "hard_skills": profile_status["hard_skills"],
        "soft_skills": profile_status["soft_skills"],
        "company_id": company_id,
        "company_name": engine._get_company_name(company_id) if company_id else "",
        "missing_requirements": engine._get_missing_requirements(profile_status["has_skills"], has_internship),
        "completion_percentage": engine._calculate_completion_percentage(profile_status["has_skills"], has_internship),
        "can_get_recommendations": profile_status["has_skills"] and has_internship,
    }

def invalidate_student_cache(student_id: str, update_type: str = "manual"):
    """
    Manually invalidate student cache (for external triggers)
    """
    ModelCache.invalidate_on_student_update(student_id, update_type)

def get_student_learning_analytics(student_id: str) -> Dict[str, Any]:
    """
    Get comprehensive learning analytics for student (only if profile complete)
    """
    engine = EnhancedCourseRecommendationEngine()
    
    # Get current recommendations (will be empty if profile incomplete)
    rec_data = engine.get_recommendations(student_id)
    
    # Get profile status
    profile_status = check_student_profile_status(student_id)
    
    # Only calculate analytics if profile is complete
    recommendations = rec_data.get("recommendations", [])
    
    analytics = {
        "profile_status": profile_status,
        "recommendation_summary": {
            "total_recommendations": len(recommendations),
            "high_priority": len([r for r in recommendations if r.get("priority") == "High"]),
            "medium_priority": len([r for r in recommendations if r.get("priority") == "Medium"]),
            "low_priority": len([r for r in recommendations if r.get("priority") == "Low"]),
        },
        "skill_analysis": {
            "total_skill_gaps": len(rec_data.get("skill_gap", [])),
            "skills_covered_by_recommendations": len(set().union(*[r.get("covers_skills", []) for r in recommendations])) if recommendations else 0,
            "skills_reinforced": len(set().union(*[r.get("reinforces_skills", []) for r in recommendations])) if recommendations else 0,
            "total_skills_available": len(set().union(*[r.get("all_taught_skills", []) for r in recommendations])) if recommendations else 0,
        },
        "learning_path": {
            "immediate_courses": [r for r in recommendations[:3] if r.get("priority") == "High"],
            "foundational_courses": [r for r in recommendations if r.get("priority") == "Medium"],
            "advanced_courses": [r for r in recommendations if r.get("priority") == "Low"],
        },
        "metadata": {
            "generated_at": time.time(),
            "cache_status": rec_data.get("metadata", {}).get("cache_used", False),
            "recommendation_engine_version": "enhanced_strict_v2.0"
        }
    }
    
    return analytics

# ============================================================
# MAIN WRAPPER FUNCTION - STRICT VERSION
# ============================================================

def run_course_recommendation(student_id: str, bypass_validation: bool = False) -> Dict[str, Any]:
    """
    STRICT: Main wrapper function - NO BYPASS allowed
    
    Args:
        student_id: Student ID
        bypass_validation: IGNORED - validation always enforced
        
    Returns:
        Dict dengan recommendation data (empty if profile incomplete)
    """
    # Get main recommendations (bypass_validation is ignored)
    recommendations = get_course_recommendations(student_id, bypass_validation=False)
    
    # Only get additional analytics if profile is complete AND has recommendations
    if (recommendations.get("has_skills") and 
        recommendations.get("has_internship") and 
        recommendations.get("recommendations")):
        
        try:
            analytics = get_student_learning_analytics(student_id)
            recommendations["learning_analytics"] = analytics
        except Exception as e:
            logger.warning("Failed to get learning analytics for student %s: %s", student_id, e)
    
    return recommendations


# ============================================================
# UTILITY FUNCTIONS - ENHANCED
# ============================================================

_PRIORITY_RANK = {'low': 0, 'medium': 1, 'high': 2}

def _dedupe_recommendations(recs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Deduplicate course recommendations based on course_id,
    keeping the one with higher score or priority.
    DEPRECATED: Use _dedupe_recommendations_with_skill_merge instead
    """
    if not recs:
        return []

    dedup = {}
    for r in recs:
        if not isinstance(r, dict):
            continue
        cid = r.get('course_id')
        if cid is None:
            cid = f"__{r.get('course_name', '')}"

        keep = dedup.get(cid)
        if keep is None:
            dedup[cid] = r
        else:
            if r.get('score', 0) > keep.get('score', 0):
                dedup[cid] = r
            elif r.get('score', 0) == keep.get('score', 0):
                kp = _PRIORITY_RANK.get(str(keep.get('priority', '')).lower(), 0)
                rp = _PRIORITY_RANK.get(str(r.get('priority', '')).lower(), 0)
                if rp > kp:
                    dedup[cid] = r

    out = list(dedup.values())
    out.sort(key=lambda x: x.get('score', 0), reverse=True)
    return out

def get_unenrolled_courses(enrolled_ids):
    """
    DEPRECATED: Use _get_targeted_courses_for_company_skills instead
    """
    if enrolled_ids:
        sql = """
            SELECT c.course_id, c.subject, c.concentration, c.curriculum, c.sks, c.type, c.semester,
                   sm.hard_skill
            FROM public.course c
            LEFT JOIN public.skill_map sm ON sm.course_id = c.course_id
            WHERE NOT (c.course_id = ANY(%s::int[]))
            ORDER BY c.semester DESC
        """
        params = [enrolled_ids]
    else:
        sql = """
            SELECT c.course_id, c.subject, c.concentration, c.curriculum, c.sks, c.type, c.semester,
                   sm.hard_skill
            FROM public.course c
            LEFT JOIN public.skill_map sm ON sm.course_id = c.course_id
            ORDER BY c.semester DESC
        """
        params = []
    return sql, params


# ============================================================
# VALIDATION HELPER FUNCTIONS
# ============================================================

def validate_student_profile_completeness(student_id: str) -> Dict[str, Any]:
    """
    NEW: Comprehensive validation of student profile completeness
    
    Returns:
        Dict with detailed validation results
    """
    validation_result = {
        "is_complete": False,
        "has_minimum_skills": False,
        "has_internship_selection": False,
        "skills_count": 0,
        "missing_requirements": [],
        "validation_details": {
            "skills_validation": {
                "passed": False,
                "hard_skills_count": 0,
                "soft_skills_count": 0,
                "total_skills_count": 0,
                "minimum_required": MIN_SKILLS_REQUIRED
            },
            "internship_validation": {
                "passed": False,
                "has_selection": False,
                "company_id": None,
                "company_name": ""
            }
        }
    }
    
    try:
        # Check skills
        with connection.cursor() as cur:
            cur.execute("""
                SELECT hard_skill, soft_skill
                FROM public.studentskill
                WHERE student_id = %s
            """, [student_id])
            
            skills_row = cur.fetchone()
            hard_skills_count = 0
            soft_skills_count = 0
            
            if skills_row:
                if skills_row[0] and skills_row[0].strip():
                    hard_skills_count = len([s.strip() for s in skills_row[0].split(",") if s.strip()])
                if skills_row[1] and skills_row[1].strip():
                    soft_skills_count = len([s.strip() for s in skills_row[1].split(",") if s.strip()])
            
            total_skills = hard_skills_count + soft_skills_count
            has_minimum_skills = total_skills >= MIN_SKILLS_REQUIRED
            
            validation_result["skills_count"] = total_skills
            validation_result["has_minimum_skills"] = has_minimum_skills
            validation_result["validation_details"]["skills_validation"].update({
                "passed": has_minimum_skills,
                "hard_skills_count": hard_skills_count,
                "soft_skills_count": soft_skills_count,
                "total_skills_count": total_skills
            })
            
            if not has_minimum_skills:
                validation_result["missing_requirements"].append(
                    f"Add {MIN_SKILLS_REQUIRED - total_skills} more skills to your profile"
                )
            
            # Check internship selection
            cur.execute("""
                SELECT company_id
                FROM public.student_company_choice
                WHERE student_id = %s
                ORDER BY created_at DESC
                LIMIT 1
            """, [student_id])
            
            internship_row = cur.fetchone()
            has_internship = bool(internship_row and internship_row[0])
            company_id = internship_row[0] if internship_row else None
            company_name = ""
            
            if has_internship:
                # Get company name
                cur.execute("""
                    SELECT company_name
                    FROM public.company_requirement
                    WHERE cr_id = %s
                """, [company_id])
                
                company_row = cur.fetchone()
                company_name = company_row[0] if company_row else f"Company ID: {company_id}"
            
            validation_result["has_internship_selection"] = has_internship
            validation_result["validation_details"]["internship_validation"].update({
                "passed": has_internship,
                "has_selection": has_internship,
                "company_id": company_id,
                "company_name": company_name
            })
            
            if not has_internship:
                validation_result["missing_requirements"].append("Select an internship company")
            
            # Overall completion
            validation_result["is_complete"] = has_minimum_skills and has_internship
            
    except Exception as e:
        logger.error("Error validating student profile %s: %s", student_id, e)
        validation_result["missing_requirements"].append("Profile validation failed - please try again")
    
    return validation_result

def get_recommendation_eligibility(student_id: str) -> Dict[str, Any]:
    """
    NEW: Check if student is eligible for recommendations
    
    Returns:
        Dict with eligibility status and detailed information
    """
    validation = validate_student_profile_completeness(student_id)
    
    eligibility = {
        "eligible": validation["is_complete"],
        "can_get_recommendations": validation["is_complete"],
        "blocking_factors": validation["missing_requirements"],
        "profile_completion_percentage": 0,
        "next_steps": [],
        "validation_summary": validation
    }
    
    # Calculate completion percentage
    completion_factors = [
        validation["has_minimum_skills"],
        validation["has_internship_selection"]
    ]
    eligibility["profile_completion_percentage"] = (sum(completion_factors) / len(completion_factors)) * 100
    
    # Build next steps
    if not validation["has_minimum_skills"]:
        skills_needed = MIN_SKILLS_REQUIRED - validation["skills_count"]
        eligibility["next_steps"].append(f"Add {skills_needed} more skills to your profile")
    
    if not validation["has_internship_selection"]:
        eligibility["next_steps"].append("Select an internship company from available options")
    
    if validation["is_complete"]:
        eligibility["next_steps"].append("Your profile is complete! You can now get personalized course recommendations.")
    
    return eligibility


# ============================================================
# DEBUGGING AND LOGGING UTILITIES
# ============================================================

def debug_recommendation_process(student_id: str, company_id: Optional[str] = None) -> Dict[str, Any]:
    """
    NEW: Debug function to trace recommendation generation process
    """
    debug_info = {
        "student_id": student_id,
        "company_id": company_id,
        "timestamp": time.time(),
        "steps": {},
        "errors": [],
        "warnings": []
    }
    
    try:
        engine = EnhancedCourseRecommendationEngine()
        
        # Step 1: Profile validation
        debug_info["steps"]["1_profile_validation"] = {
            "status": "checking",
            "profile_status": engine._get_comprehensive_profile_status(student_id),
            "company_selection": engine._get_student_selected_company(student_id)
        }
        
        # Step 2: Company skills
        if company_id or debug_info["steps"]["1_profile_validation"]["company_selection"]:
            cid = company_id or debug_info["steps"]["1_profile_validation"]["company_selection"]
            debug_info["steps"]["2_company_skills"] = {
                "company_id": cid,
                "company_name": engine._get_company_name(cid),
                "required_skills": engine._get_company_required_skills(cid)
            }
        
        # Step 3: Student data
        student_data = engine._get_student_data(student_id)
        debug_info["steps"]["3_student_data"] = {
            "has_data": bool(student_data),
            "skills_summary": {
                "hard_skills_count": len(student_data.get("skills", {}).get("hard", [])) if student_data else 0,
                "soft_skills_count": len(student_data.get("skills", {}).get("soft", [])) if student_data else 0,
                "enrollments_count": len(student_data.get("enrollments", {})) if student_data else 0
            } if student_data else None
        }
        
        # Step 4: Skill gap analysis
        if company_id or debug_info["steps"]["1_profile_validation"]["company_selection"]:
            cid = company_id or debug_info["steps"]["1_profile_validation"]["company_selection"]
            skill_gaps = engine._get_skill_gap_for_student_company(student_id, cid)
            debug_info["steps"]["4_skill_gap"] = {
                "gaps_found": len(skill_gaps),
                "missing_skills": skill_gaps
            }
        
        # Step 5: Recommendation generation attempt
        try:
            recommendations = engine.get_recommendations(student_id, company_id)
            debug_info["steps"]["5_recommendations"] = {
                "generated": len(recommendations.get("recommendations", [])),
                "has_skills": recommendations.get("has_skills"),
                "has_internship": recommendations.get("has_internship"),
                "message": recommendations.get("message"),
                "cache_used": recommendations.get("metadata", {}).get("cache_used", False)
            }
        except Exception as e:
            debug_info["errors"].append(f"Recommendation generation failed: {str(e)}")
            debug_info["steps"]["5_recommendations"] = {"error": str(e)}
        
    except Exception as e:
        debug_info["errors"].append(f"Debug process failed: {str(e)}")
    
    return debug_info

def log_recommendation_metrics(student_id: str, recommendations: List[Dict[str, Any]], 
                              processing_time: float, cache_hit: bool = False):
    """
    NEW: Log recommendation metrics for monitoring
    """
    metrics = {
        "student_id": student_id,
        "timestamp": time.time(),
        "processing_time_ms": round(processing_time * 1000, 2),
        "cache_hit": cache_hit,
        "recommendations_count": len(recommendations),
        "priority_distribution": {
            "high": len([r for r in recommendations if r.get("priority") == "High"]),
            "medium": len([r for r in recommendations if r.get("priority") == "Medium"]),
            "low": len([r for r in recommendations if r.get("priority") == "Low"])
        },
        "average_score": round(sum(r.get("score", 0) for r in recommendations) / len(recommendations), 2) if recommendations else 0,
        "skill_coverage": {
            "total_gaps_covered": len(set().union(*[r.get("covers_skills", []) for r in recommendations])) if recommendations else 0,
            "total_skills_reinforced": len(set().union(*[r.get("reinforces_skills", []) for r in recommendations])) if recommendations else 0,
        }
    }
    
    logger.info(
        "[METRICS] Student=%s | Recs=%d | Time=%sms | Cache=%s | AvgScore=%.2f | GapsCovered=%d",
        student_id,
        metrics["recommendations_count"],
        metrics["processing_time_ms"],
        "HIT" if cache_hit else "MISS",
        metrics["average_score"],
        metrics["skill_coverage"]["total_gaps_covered"]
    )