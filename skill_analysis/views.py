from django.shortcuts import render, redirect, get_object_or_404
from django.db import connection
from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Q, Count, Exists, OuterRef  
from .models import Student, Skill, StudentSkill, Certificate, CompanyRequirement, CompanyRequirementSkill, StudentCompanyChoice
from django.http import Http404, JsonResponse, HttpResponseNotFound
import os
import json
from django.utils.timezone import now
from django.conf import settings
from django.core.files.storage import FileSystemStorage
import time
from django.views.decorators.csrf import csrf_exempt
from django.db.models import Count
from django.db.models.functions import ExtractYear
from collections import defaultdict

from .train_random_forest import (
    run_course_recommendation as get_course_recommendations,
    get_skill_gap_for_student_company, 
    ModelCache, 
    _dedupe_recommendations,
)
from .models import (
    Student, StudentSkill, Certificate, 
    CompanyRequirement, CompanyRequirementSkill, 
    Skill, Course, StudentCompanyChoice
)

def home_student_view(request):
    student_id = request.session.get('student_id')
    if not student_id:
        return redirect('login')

    internship = StudentCompanyChoice.objects.filter(student_id=student_id).first()
    company_name = "-"
    position = "-"

    if internship:
        company_name = internship.company.company_name
        position = internship.company.position

    certificates = Certificate.objects.filter(student_id=student_id)

    # Ambil skill gap dari fungsi rekomendasi
    skill_gaps = get_skill_gap_for_student_company(str(student_id)) or []

    # Persentase skill
    skill_fulfilled = calculate_skill_process(student_id)
    skill_not_fulfilled = round(100 - skill_fulfilled, 2)

    context = {
        'company_name': company_name,
        'position': position,
        'certificates': certificates,
        'skill_fulfilled_percent': skill_fulfilled,
        'skill_not_fulfilled_percent': skill_not_fulfilled,
        'skill_gaps': skill_gaps,  # ✅ Tambahkan skill_gaps ke context
    }

    return render(request, 'skill_analysis/home_student.html', context)

def home_icc_view(request):
    total_students = Student.objects.count()

    # Company paling diminati
    company_counts = (
        StudentCompanyChoice.objects
        .values('company__company_name')
        .annotate(count=Count('company'))
        .order_by('-count')[:5]
    )
    company_data = {
        'labels': [item['company__company_name'] for item in company_counts],
        'data': [item['count'] for item in company_counts]
    }

    # Posisi paling diminati
    position_counts = (
        StudentCompanyChoice.objects
        .values('position')
        .annotate(count=Count('position'))
        .order_by('-count')[:5]
    )
    position_data = {
        'labels': [item['position'] for item in position_counts],
        'data': [item['count'] for item in position_counts]
    }

    # Hard skill paling banyak dibutuhkan oleh perusahaan yang dipilih
    choices_subquery = StudentCompanyChoice.objects.filter(
        company_id=OuterRef('cr_id')
    )

    # ✨ FIX: ubah cara penulisan filter
    hard_skill_counts = (
        CompanyRequirementSkill.objects
        .filter(
            skill_type__iexact='hard skill'
        )
        .filter(  # tambahkan filter kedua untuk Exists()
            Exists(choices_subquery)
        )
        .values('skill__skill_name')
        .annotate(count=Count('skill'))
        .order_by('-count')[:5]
    )

    hard_skill_data = {
        'labels': [item['skill__skill_name'] for item in hard_skill_counts],
        'data': [item['count'] for item in hard_skill_counts]
    }

    return render(request, 'skill_analysis/home_icc.html', {
        'total_students': total_students,
        'company_data': company_data,
        'position_data': position_data,
        'hard_skill_data': hard_skill_data
    })

def analysis_view(request):
    # Ambil semua tahun unik dari created_at
    all_years = StudentCompanyChoice.objects.annotate(
        year=ExtractYear('created_at')
    ).values_list('year', flat=True).distinct().order_by('-year')

    # Tahun terbaru
    latest_year = all_years[0] if all_years else None

    # === Filter tahun masing-masing card ===
    year_position = request.GET.get('year_position') or latest_year
    year_company = request.GET.get('year_company') or latest_year
    year_list_of_position = request.GET.get('year_list_of_position') or latest_year
    trend_years = request.GET.getlist('trend_years')

    # Jika trend_years tidak dipilih, default ke 3 tahun terbaru
    if not trend_years:
        recent_years = list(all_years)[:3]
        trend_years = [str(y) for y in recent_years]

    # === MOST POPULAR POSITION ===
    most_position_data = []
    if year_position:
        choices = StudentCompanyChoice.objects.filter(created_at__year=year_position)
        total_choices = choices.count()
        positions = choices.values('position').annotate(count=Count('position')).order_by('-count')[:5]

        for item in positions:
            percent = round((item['count'] / total_choices) * 100, 1) if total_choices > 0 else 0
            most_position_data.append({
                'position': item['position'],
                'label': f"{item['count']} Student – {percent}% Applicants"
            })

    # === MOST POPULAR COMPANY ===
    most_company_data = []
    if year_company:
        choices = StudentCompanyChoice.objects.filter(created_at__year=year_company)
        total_choices = choices.count()
        companies = choices.values('company__company_name').annotate(count=Count('company')).order_by('-count')[:5]

        for item in companies:
            percent = round((item['count'] / total_choices) * 100, 1) if total_choices > 0 else 0
            most_company_data.append({
                'company__company_name': item['company__company_name'],
                'label': f"{item['count']} Student – {percent}% Applicants"
            })

    # === TREND LINE ===
    chart_labels = []
    chart_counts = []
    chart_tooltips = []

    for y in trend_years:
        count = StudentCompanyChoice.objects.filter(created_at__year=y).count()
        chart_labels.append(str(y))
        chart_counts.append(count)
        chart_tooltips.append(f"{count} students")

    # === COMPANY & POSITION LIST ===
    companies = CompanyRequirement.objects \
        .exclude(company_name__isnull=True) \
        .values_list('company_name', flat=True) \
        .distinct()

    selected_company = request.GET.get('company') or (companies[0] if companies else None)

    positions = []
    position_data = []

    if selected_company:
        # Ambil daftar posisi dari CompanyRequirement
        positions = CompanyRequirement.objects.filter(company_name=selected_company) \
            .exclude(position__isnull=True).values_list('position', flat=True).distinct()

        # Ambil data student yang memilih company tersebut pada tahun terpilih
        student_choices = StudentCompanyChoice.objects.filter(
            company__company_name=selected_company,
            created_at__year=year_list_of_position
        )
        total_students = student_choices.count()

        position_counts = defaultdict(int)
        for sc in student_choices:
            if sc.position:
                position_counts[sc.position] += 1

        for pos in positions:
            count = position_counts.get(pos, 0)
            percent = round((count / total_students) * 100, 1) if total_students > 0 else 0
            position_data.append({
                'position': pos,
                'count': count,
                'percent': percent
            })

        # 🔽 Sortir posisi dari count terbesar ke terkecil
        position_data = sorted(position_data, key=lambda x: x['count'], reverse=True)

    return render(request, 'skill_analysis/analysis.html', {
        'all_years': all_years,
        'year_position': int(year_position) if year_position else None,
        'year_company': int(year_company) if year_company else None,
        'year_list_of_position': int(year_list_of_position) if year_list_of_position else None,
        'selected_trend_years': [int(y) for y in trend_years],

        # Data utama
        'most_position_data': most_position_data,
        'most_company_data': most_company_data,
        'chart_labels': chart_labels,
        'chart_counts': chart_counts,
        'chart_tooltips': chart_tooltips,

        # List posisi per perusahaan
        'companies': companies,
        'selected_company': selected_company,
        'positions': positions,
        'position_data': position_data,
    })

def course_view(request):
    return render(request, 'skill_analysis/course.html')

def student_view(request):
    student_id = request.session.get('student_id')

    if not student_id:
        return redirect('login')

    try:
        student = Student.objects.get(student_id=student_id)
    except Student.DoesNotExist:
        return render(request, 'skill_analysis/student.html', {'error': 'Student not found'})

    student_skills = StudentSkill.objects.filter(student_id=student_id)

    internship = StudentCompanyChoice.objects.filter(student_id=student_id).first()
    company_name = "-"
    position = "-"

    if internship and internship.company_id:
        company = CompanyRequirement.objects.filter(cr_id=internship.company_id).first()
        if company:
            company_name = company.company_name or "-"
            position = company.position or "-"

    hard_skills = []
    soft_skills = []

    for skill in student_skills:
        if skill.hard_skill:
            hard_skills.extend([s.strip() for s in skill.hard_skill.split(',') if s.strip()])
        if skill.soft_skill:
            soft_skills.extend([s.strip() for s in skill.soft_skill.split(',') if s.strip()])

    skill_fulfilled = calculate_skill_process(student_id)
    skill_not_fulfilled = round(100 - skill_fulfilled, 2)
    skill_gaps = get_skill_gap_for_student_company(str(student_id)) or []

    return render(request, 'skill_analysis/student.html', {
        'student': student,
        'hard_skills': hard_skills,
        'soft_skills': soft_skills,
        'company_name': company_name,
        'position': position,
        'skill_fulfilled_percent': skill_fulfilled,
        'skill_not_fulfilled_percent': skill_not_fulfilled,
        'skill_gaps': skill_gaps,
    })
    
def update_profile_photo(request):
    if request.method == 'POST' and request.FILES.get('image'):
        student_id = request.session.get('student_id')
        if not student_id:
            return redirect('login')

        student = Student.objects.get(student_id=student_id)
        uploaded_file = request.FILES['image']

        fs = FileSystemStorage(location=os.path.join(settings.MEDIA_ROOT, 'student_photos'))
        filename = fs.save(uploaded_file.name, uploaded_file)

        # Simpan path relatif ke field image
        student.image = f"/media/student_photos/{filename}"
        student.save()

        return redirect('student')

    return redirect('student')

def student_icc_view(request):
    query = request.GET.get('q', '')
    batch = request.GET.get('batch', '')

    students = Student.objects.all()

    if query:
        students = students.filter(
            Q(full_name__icontains=query) |
            Q(student_id__icontains=query)
        )

    if batch:
        students = students.filter(batch=batch)

    batches = Student.objects.values_list('batch', flat=True).distinct()

    paginator = Paginator(students, 20)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    return render(request, 'skill_analysis/student_icc.html', {
        'students': page_obj,
        'query': query,
        'batch': batch,
        'batches': batches,
    })

def student_data_view(request, student_id):
    student = get_object_or_404(Student, student_id=student_id)
    student_skills = StudentSkill.objects.filter(student_id=student_id)

    internship = StudentCompanyChoice.objects.filter(student_id=student_id).first()
    company_name = "-"
    position = "-"

    if internship and internship.company_id:
        company = CompanyRequirement.objects.filter(cr_id=internship.company_id).first()
        if company:
            company_name = company.company_name or "-"
            position = company.position or "-"

    hard_skills = []
    soft_skills = []

    for skill in student_skills:
        if skill.hard_skill:
            hard_skills.extend([s.strip() for s in skill.hard_skill.split(',') if s.strip()])
        if skill.soft_skill:
            soft_skills.extend([s.strip() for s in skill.soft_skill.split(',') if s.strip()])

    skill_fulfilled = calculate_skill_process(student_id)
    skill_not_fulfilled = round(100 - skill_fulfilled, 2)
    skill_gaps = get_skill_gap_for_student_company(str(student_id)) or []

    return render(request, 'skill_analysis/student_data.html', {
        'student': student,
        'hard_skills': hard_skills,
        'soft_skills': soft_skills,
        'company_name': company_name,
        'position': position,
        'skill_fulfilled_percent': skill_fulfilled,
        'skill_not_fulfilled_percent': skill_not_fulfilled,
        'skill_gaps': skill_gaps,
    })

def calculate_skill_process(student_id):
    # Ambil company yang dipilih student
    choice = StudentCompanyChoice.objects.filter(student_id=student_id).first()
    if not choice or not choice.company_id:
        return 0.0  # jika belum memilih, return 0

    # Ambil skill requirement dari posisi tersebut
    requirements = CompanyRequirementSkill.objects.filter(cr_id=choice.company_id)
    required_skills = [r.skill.skill_name.strip().lower() for r in requirements if r.skill]

    if not required_skills:
        return 0.0

    # Ambil skill student dari StudentSkill (tanpa sertifikat)
    skills = StudentSkill.objects.filter(student_id=student_id)
    student_hard = set()
    student_soft = set()

    for s in skills:
        if s.hard_skill:
            student_hard.update([x.strip().lower() for x in s.hard_skill.split(',')])
        if s.soft_skill:
            student_soft.update([x.strip().lower() for x in s.soft_skill.split(',')])

    # Hitung bobot (tanpa mempertimbangkan sertifikat)
    score = 0
    max_score = len(required_skills)  # 1.0 per skill

    for skill in required_skills:
        if skill in student_hard or skill in student_soft:
            score += 1.0

    return round((score / max_score) * 100, 2)

def _get_student_for_request(request):
    """Helper function untuk mengambil student dari session"""
    student_id = request.session.get('student_id')
    if not student_id:
        return None
    try:
        return Student.objects.get(student_id=student_id)
    except Student.DoesNotExist:
        return None

def learning_view(request):
    """
    Enhanced learning view with course recommendations
    """
    student = _get_student_for_request(request)
    if not student:
        return render(request, 'skill_analysis/learning.html', {
            'error': 'Student not found. Please login.',
            'recommendations': [],
            'skill_gaps': [],
        })

    # Upload Certificate (existing functionality)
    if request.method == 'POST' and request.FILES.get('certificate_file'):
        try:
            file = request.FILES['certificate_file']
            skill_type = request.POST.get('skill_type', '')
            skill_name = request.POST.get('skill_name', '')
            certificate_name = request.POST.get('certificate_name', file.name)

            if not all([skill_type, skill_name]):
                messages.error(request, "Skill type and name are required.")
                return redirect('learning')

            Certificate.objects.create(
                student_id=student.student_id,
                file=file,
                skill_type=skill_type,
                skill_name=skill_name,
                certificate_name=certificate_name
            )
            # ✅ Invalidate cache after certificate upload
            ModelCache.invalidate_all(str(student.student_id))
            messages.success(request, "Certificate uploaded successfully.")
            return redirect('learning')
        except Exception as e:
            messages.error(request, f"Error uploading certificate: {e}")
            return redirect('learning')

    # ✅ ENHANCED: Get course recommendations and skill gaps
    try:
        # Get recommendations using ML function
        recommendations = get_course_recommendations(str(student.student_id)) or []
        
        # Get skill gaps for the student
        skill_gaps = get_skill_gap_for_student_company(str(student.student_id)) or []
        
        # Deduplicate recommendations if needed
        if recommendations:
            recommendations = _dedupe_recommendations(recommendations)
            
    except Exception as e:
        print(f"Error getting recommendations: {e}")  # For debugging
        recommendations = []
        skill_gaps = []
        messages.error(request, f"Error loading recommendations: {e}")

    # ✅ ENHANCED: Prepare context with all data
    context = {
        'recommendations': recommendations,
        'skill_gaps': skill_gaps,
        'student_id': str(student.student_id),
        'total_recommendations': len(recommendations),
        'student': student,
    }

    return render(request, 'skill_analysis/learning.html', context)

def course_recommendations_view(request):
    """
    Dedicated view untuk menampilkan halaman rekomendasi course
    """
    student = _get_student_for_request(request)
    if not student:
        return redirect('login')
    
    try:
        # Ambil rekomendasi course menggunakan run_course_recommendation
        recommendations = get_course_recommendations(str(student.student_id))
        
        # Ambil skill gap 
        skill_gaps = get_skill_gap_for_student_company(str(student.student_id))
        
        # Deduplicate recommendations
        if recommendations:
            recommendations = _dedupe_recommendations(recommendations)
        
        context = {
            'recommendations': recommendations or [],
            'skill_gaps': skill_gaps or [],
            'student_id': str(student.student_id),
            'total_recommendations': len(recommendations) if recommendations else 0,
            'student': student,
        }
        
        return render(request, 'skill_analysis/learning.html', context)
        
    except Exception as e:
        context = {
            'error': f'Gagal memuat rekomendasi: {str(e)}',
            'recommendations': [],
            'skill_gaps': [],
            'student': student,
        }
        return render(request, 'skill_analysis/learning.html', context)

def api_course_recommendations(request):
    """
    API endpoint untuk mendapat rekomendasi course (JSON response)
    """
    student = _get_student_for_request(request)
    if not student:
        return JsonResponse({'success': False, 'error': 'Student not found'}, status=401)
        
    if request.method == 'GET':
        try:
            recommendations = get_course_recommendations(str(student.student_id))
            skill_gaps = get_skill_gap_for_student_company(str(student.student_id))
            
            # Deduplicate recommendations
            if recommendations:
                recommendations = _dedupe_recommendations(recommendations)
            
            return JsonResponse({
                'success': True,
                'data': {
                    'recommendations': recommendations or [],
                    'skill_gaps': skill_gaps or [],
                    'total': len(recommendations) if recommendations else 0
                }
            })
            
        except Exception as e:
            return JsonResponse({
                'success': False,
                'error': str(e)
            }, status=500)
    
    return JsonResponse({'error': 'Method not allowed'}, status=405)

@csrf_exempt
def refresh_recommendations(request):
    """
    API endpoint untuk refresh/clear cache rekomendasi
    """
    student = _get_student_for_request(request)
    if not student:
        return JsonResponse({'success': False, 'error': 'Student not found'}, status=401)
        
    if request.method == 'POST':
        try:
            # Clear cache untuk student ini
            ModelCache.invalidate_all(str(student.student_id))
            
            return JsonResponse({
                'success': True,
                'message': 'Cache berhasil di-refresh. Rekomendasi akan diperbarui.'
            })
            
        except Exception as e:
            return JsonResponse({
                'success': False,
                'error': str(e)
            }, status=500)
    
    return JsonResponse({'error': 'Method not allowed'}, status=405)

def delete_certificate_learning(request, certificate_id):
    student = _get_student_for_request(request)
    if not student:
        return redirect('login')
        
    try:
        cert = get_object_or_404(Certificate, pk=certificate_id, student_id=student.student_id)
        if cert.file:
            cert.file.delete(save=False)
        cert.delete()
        # ✅ FIXED: Invalidate cache with correct student_id type
        ModelCache.invalidate_all(str(student.student_id))
        messages.success(request, "Certificate deleted successfully.")
    except Exception as e:
        messages.error(request, f"Error deleting certificate: {e}")
        
    return redirect('learning')

def upload_certificate_learning(request):
    """Handle certificate upload in learning page"""
    student = _get_student_for_request(request)
    if not student:
        return redirect('login')

    if request.method == 'POST':
        try:
            file = request.FILES.get('certificate_file')
            skill_type = request.POST.get('skill_type', '')
            skill_name = request.POST.get('skill_name', '')
            certificate_name = request.POST.get('certificate_name', '')

            if not file:
                messages.error(request, "Please select a file to upload.")
                return redirect('learning')

            if not all([skill_type, skill_name]):
                messages.error(request, "Skill type and name are required.")
                return redirect('learning')

            if not certificate_name:
                certificate_name = file.name

            Certificate.objects.create(
                student_id=student.student_id,
                file=file,
                skill_type=skill_type,
                skill_name=skill_name,
                certificate_name=certificate_name
            )
            
            # ✅ Invalidate cache after certificate upload
            ModelCache.invalidate_all(str(student.student_id))
            messages.success(request, "Certificate uploaded successfully.")
            
        except Exception as e:
            messages.error(request, f"Error uploading certificate: {e}")
    
    return redirect('learning')

def recommendations_api(request):
    """Legacy API endpoint - redirects to new API"""
    return api_course_recommendations(request)

def skill_icc_view(request):
    return render(request, 'skill_analysis/skill_icc.html')

def internship_view(request):
    companies = CompanyRequirement.objects.values_list('company_name', flat=True).distinct()
    return render(request, 'skill_analysis/internship.html', {'companies': companies})

def internship_desc_view(request, company_name):
    company_requirements = CompanyRequirement.objects.filter(company_name=company_name)
    skills = CompanyRequirementSkill.objects.filter(
        cr_id__in=company_requirements.values_list('cr_id', flat=True)
    )

    all_skill_ids = skills.values_list('skill_id', flat=True).distinct()
    skill_dict = {
        s.skill_id: s.skill_name
        for s in Skill.objects.filter(skill_id__in=all_skill_ids)
    }

    positions_data = {}
    for req in company_requirements:
        soft_ids = skills.filter(cr_id=req.cr_id, skill_type__icontains='Soft Skill').values_list('skill_id', flat=True)
        hard_ids = skills.filter(cr_id=req.cr_id, skill_type__icontains='Hard Skill').values_list('skill_id', flat=True)

        soft_names = [skill_dict.get(skill_id, skill_id) for skill_id in soft_ids]
        hard_names = [skill_dict.get(skill_id, skill_id) for skill_id in hard_ids]

        positions_data[req.position] = {
            'desc': req.job_desc,
            'soft': ', '.join(soft_names),
            'hard': ', '.join(hard_names),
            'cr_id': req.cr_id  # simpan cr_id juga untuk simpan ke DB
        }

    # ✅ Blok POST — cek dan proses pilihan
    if request.method == "POST":
        student_id = request.session.get('student_id')
        position = request.POST.get('position')

        if not student_id:
            messages.error(request, "You must login first.")
            return redirect('login')

        if position not in positions_data:
            messages.error(request, "Invalid position selected.")
            return redirect('apply', company_name=company_name)

        cr_id = positions_data[position]['cr_id']

        try:
            existing_choice = StudentCompanyChoice.objects.filter(student_id=student_id).first()
            if existing_choice:
                # Update pilihan lama
                existing_choice.company_id = cr_id
                existing_choice.position = position
                existing_choice.created_at = now()
                existing_choice.save()
                messages.success(request, "Your previous choice has been updated.")
            else:
                # Buat pilihan baru
                StudentCompanyChoice.objects.create(
                    student_id=student_id,
                    company_id=cr_id,
                    position=position,
                    created_at=now()
                )
                messages.success(request, "You successfully chose this company.")

            return redirect('internship_desc', company_name=company_name)

        except Exception as e:
            messages.error(request, f"Failed to save your choice: {str(e)}")
            return redirect('internship_desc', company_name=company_name)

    context = {
        'company_name': company_name,
        'positions': list(positions_data.keys()),
        'positions_data': json.dumps(positions_data)
    }
    return render(request, 'skill_analysis/internship_desc.html', context)

def login_view(request):
    if request.method == 'POST':
        email = request.POST.get('email')
        password = request.POST.get('password')  # sebenarnya ini student_id

        if email == 'admin' and password == 'staff':
            request.session['is_admin'] = True  # kalau kamu perlu tandai sebagai admin
            return redirect('home_icc') 
        
        try:
            student = Student.objects.get(email=email, student_id=password)
            request.session['student_id'] = str(student.student_id)
            messages.success(request, 'Login successful!')
            return redirect('home_student')
        except Student.DoesNotExist:
            messages.error(request, 'Invalid email or student ID')
            return redirect('login')

    return render(request, 'skill_analysis/login.html')

def logout_view(request):
    request.session.flush()
    return redirect('login') 

def skill_view(request):
    student_id = request.session.get("student_id")
    if not student_id:
        return redirect('login')

    try:
        student = Student.objects.get(student_id=student_id)
    except Student.DoesNotExist:
        messages.error(request, "Student not found.")
        return redirect('login')

    # Ambil skill dari StudentSkill
    try:
        student_skill = StudentSkill.objects.get(student=student)
        hardskill_str = student_skill.hard_skill or ""
        softskill_str = student_skill.soft_skill or ""
    except StudentSkill.DoesNotExist:
        hardskill_str = ""
        softskill_str = ""

    student_skills = []

    if hardskill_str:
        for skill in hardskill_str.split(','):
            student_skills.append({'skill_name': skill.strip(), 'skill_type': 'Hard Skill'})
    if softskill_str:
        for skill in softskill_str.split(','):
            student_skills.append({'skill_name': skill.strip(), 'skill_type': 'Soft Skill'})

    skill_types = Skill.objects.values_list('skill_type', flat=True).distinct()
    skills = Skill.objects.all()

    certificates = Certificate.objects.filter(student=student)

    return render(request, 'skill_analysis/skill.html', {
        'student_skills': student_skills,
        'skill_types': skill_types,
        'skills': skills,
        'certificates': certificates,
    })

def add_skill(request):
    if request.method == 'POST':
        skill_type = request.POST.get('skill_type')
        skill_name = request.POST.get('skill_name')
        student_id = request.session.get('student_id')

        if not student_id:
            return redirect('login')

        try:
            student = Student.objects.get(student_id=student_id)
        except Student.DoesNotExist:
            messages.error(request, "Student not found.")
            return redirect('login')

        student_skill, created = StudentSkill.objects.get_or_create(student=student)

        if skill_type == 'Hard Skill':
            existing = student_skill.hard_skill or ""
            skills = [s.strip() for s in existing.split(',') if s.strip()]
            if skill_name not in skills:
                skills.append(skill_name)
            student_skill.hard_skill = ', '.join(skills)

        elif skill_type == 'Soft Skill':
            existing = student_skill.soft_skill or ""
            skills = [s.strip() for s in existing.split(',') if s.strip()]
            if skill_name not in skills:
                skills.append(skill_name)
            student_skill.soft_skill = ', '.join(skills)

        student_skill.save()
        return redirect('skill')

    return redirect('skill')

def get_skills_by_type(request):
    skill_type = request.GET.get('skill_type')
    skills = Skill.objects.filter(skill_type=skill_type).values_list('skill_name', flat=True)
    return JsonResponse(list(skills), safe=False)

def add_certificate(request):
    if request.method == 'POST':
        try:
            student_id = request.session.get('student_id')
            student = Student.objects.get(pk=student_id)

            skill_type = request.POST.get('skill_type')
            skill_name = request.POST.get('skill_name')
            certificate_name = request.POST.get('certificate_name')
            file = request.FILES.get('file')

            Certificate.objects.create(
                student=student,
                skill_type=skill_type,
                skill_name=skill_name,
                certificate_name=certificate_name,
                file=file
            )
            return redirect('skill')
        except Exception as e:
            print("❌ Error:", e)
            # Optional: tampilkan pesan error ke user juga
            return redirect('skill')
    return redirect('skill')

def delete_skill(request, skill_name):
    student_id = request.session.get("student_id")
    if not student_id:
        return redirect('login')

    try:
        student = Student.objects.get(student_id=student_id)
        student_skill = StudentSkill.objects.get(student=student)

        # Bersihkan dari hard/soft skill
        for attr in ['hard_skill', 'soft_skill']:
            skill_str = getattr(student_skill, attr) or ""
            skills = [s.strip() for s in skill_str.split(',') if s.strip()]
            if skill_name in skills:
                skills.remove(skill_name)
                setattr(student_skill, attr, ', '.join(skills))

        student_skill.save()
        messages.success(request, "Skill deleted.")
    except Exception as e:
        messages.error(request, f"Error deleting skill: {e}")

    return redirect('skill')

def delete_certificate(request, c_id):
    student_id = request.session.get("student_id")
    if not student_id:
        return redirect('login')

    try:
        cert = Certificate.objects.get(pk=c_id, student__student_id=student_id)
        cert.file.delete()  # delete file from disk
        cert.delete()
        messages.success(request, "Certificate deleted.")
    except Certificate.DoesNotExist:
        messages.error(request, "Certificate not found.")
    except Exception as e:
        messages.error(request, f"Error deleting certificate: {e}")

    return redirect('skill')

def skill_list(request):
    skills = Skill.objects.all()
    skill_types = Skill.objects.values_list('skill_type', flat=True).distinct()

    search_query = request.GET.get('search')
    type_query = request.GET.get('type')

    if search_query:
        skills = skills.filter(skill_name__icontains=search_query)

    if type_query:
        skills = skills.filter(skill_type=type_query)

    return render(request, 'skill_analysis/skill_icc.html', {
        'skills': skills,
        'skill_types': skill_types,
    })

def generate_next_skill_id():
    last = Skill.objects.aggregate(Max('skill_id'))['skill_id__max']
    if last:
        number = int(re.search(r'\d+', last).group())
        return f"SK{number + 1:03d}"
    else:
        return "SK001"

def add_skill_icc(request):
    if request.method == 'POST':
        skill_type = request.POST.get('skillType')
        skill_name = request.POST.get('skillName')
        skill_id = generate_next_skill_id()

        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO skill (skill_id, skill_name, skill_type) VALUES (%s, %s, %s)",
                [skill_id, skill_name, skill_type]
            )

        return redirect('skill_icc')
    else:
        return redirect('skill_icc')

def showIntern(request): 
    cr_id = CompanyRequirement.objects.all()
    internships = CompanyRequirement.objects.all()
    positions = CompanyRequirement.objects.values_list('position', flat=True).distinct()
    search_query = request.GET.get('search', '')
    position_query = request.GET.get('position', '')

    if search_query:
        internships = internships.filter(company_name__icontains=search_query)
    if position_query:
        internships = internships.filter(position=position_query)

    positions = CompanyRequirement.objects.values_list('position', flat=True).distinct()

    return render(request, 'skill_analysis/internship_icc.html', {
        'cr_id' : cr_id,
        'internships': internships,
        'positions': positions,
    })

def delete_intern(request, cr_id):
    try:
        CompanyRequirementSkill.objects.filter(cr_id=cr_id).delete()

        CompanyRequirement.objects.filter(cr_id=cr_id).delete()

        messages.success(request, "Internship data successfully deleted.")
    except Exception as e:
        messages.error(request, f"Failed to delete: {e}")
    
    return redirect('internship_icc')

def skill_internForm(request):
    hard_skills = Skill.objects.filter(skill_type='Hard Skill')
    soft_skills = Skill.objects.filter(skill_type='Soft Skill')

    return render(request, 'skill_analysis/internship_form_icc.html', {
        'hard_skills': hard_skills,
        'soft_skills': soft_skills,
    })

def generate_next_cr_id():
    last = CompanyRequirement.objects.aggregate(Max('cr_id'))['cr_id__max']
    if last:
        number = int(re.search(r'\d+', last).group())
        return f"CR{number + 1:04d}"
    else:
        return "CR0001"

def generate_next_crs_id():
    last = CompanyRequirementSkill.objects.aggregate(Max('crs_id'))['crs_id__max']
    if last:
        number = int(re.search(r'\d+', last).group())
        return f"CRS{number + 1:04d}"
    else:
        return "CRS0001"

def submit_intern(request):
    if request.method == 'POST':
        company_name = request.POST.get('companyName')
        position = request.POST.get('positionName')
        job_desc = request.POST.get('desc')

        new_cr_id = generate_next_cr_id()

        new_requirement = CompanyRequirement.objects.create(
            cr_id=new_cr_id,
            company_name=company_name,
            position=position,
            job_desc=job_desc
        )

        # Ambil list skill ID dari form
        hard_skill_ids = request.POST.getlist('hardSkills[]')  
        soft_skill_ids = request.POST.getlist('softSkills[]')  
        
        all_skill_ids = hard_skill_ids + soft_skill_ids

        for skill_id in all_skill_ids:
            try:
                skill_obj = Skill.objects.get(skill_id=skill_id)
                CompanyRequirementSkill.objects.create(
                    crs_id=generate_next_crs_id(),
                    cr=new_requirement,
                    skill=skill_obj,
                    skill_type=skill_obj.skill_type 
                )
            except Skill.DoesNotExist:
                continue 

        return redirect('internship_icc')
    hard_skills = Skill.objects.filter(skill_type='Hard Skill')
    soft_skills = Skill.objects.filter(skill_type='Soft Skill')

    return render(request, 'skill_analysis/internship_form_icc.html', {
        'hard_skills': hard_skills,
        'soft_skills': soft_skills,
    })

from django.db.models import Max
import re

def generate_new_crs_id():
    last_crs = CompanyRequirementSkill.objects.order_by('-crs_id').first()
    if last_crs and last_crs.crs_id:
        match = re.search(r'\d+', last_crs.crs_id)
        if match:
            num = int(match.group()) + 1
            return f"CRS{num:04d}"
    return "CRS0001"

def edit_intern(request, cr_id):
    try:
        internship = CompanyRequirement.objects.get(cr_id=cr_id)
    except CompanyRequirement.DoesNotExist:
        raise Http404("Data Not Found")

    hard_skills = Skill.objects.filter(skill_type='Hard Skill')
    soft_skills = Skill.objects.filter(skill_type='Soft Skill')

    selected_skills = CompanyRequirementSkill.objects.filter(cr=internship)
    selected_hard = [s.skill.skill_id for s in selected_skills if s.skill.skill_type == 'Hard Skill']
    selected_soft = [s.skill.skill_id for s in selected_skills if s.skill.skill_type == 'Soft Skill']

    if request.method == 'POST':
        internship.company_name = request.POST.get('company_name')
        internship.position = request.POST.get('position')
        internship.job_desc = request.POST.get('job_desc') 
        internship.save()

        CompanyRequirementSkill.objects.filter(cr=internship).delete()

        hard_ids = request.POST.getlist('hard_skills[]')
        soft_ids = request.POST.getlist('soft_skills[]')

        for skill_id in hard_ids:
            skill = Skill.objects.get(skill_id=skill_id)
            new_crs_id = generate_new_crs_id()
            CompanyRequirementSkill.objects.create(
                crs_id=new_crs_id,
                cr=internship,
                skill=skill,
                skill_type=skill.skill_type
            )

        for skill_id in soft_ids:
            skill = Skill.objects.get(skill_id=skill_id)
            new_crs_id = generate_new_crs_id()
            CompanyRequirementSkill.objects.create(
                crs_id=new_crs_id,
                cr=internship,
                skill=skill,
                skill_type=skill.skill_type
            )

        messages.success(request, 'Internship updated successfully.')
        return redirect('internship_icc')

    context = {
        'internship': internship,
        'hard_skills': hard_skills,
        'soft_skills': soft_skills,
        'selected_hard': selected_hard,
        'selected_soft': selected_soft,
    }
    return render(request, 'skill_analysis/internship_edit_icc.html', context)

def update_intern(request, cr_id):
    company_req = get_object_or_404(CompanyRequirement, cr_id=cr_id)

    if request.method == 'POST':
        company_req.company_name = request.POST.get('companyName')
        company_req.position = request.POST.get('positionName')
        company_req.job_desc = request.POST.get('job_desc')
        company_req.save()

        new_hard_skills = request.POST.getlist('hardSkills[]')
        new_soft_skills = request.POST.getlist('softSkills[]')

        CompanyRequirementSkill.objects.filter(cr=company_req).delete()

        for skill_id in new_hard_skills:
            skill = Skill.objects.get(skill_id=skill_id)
            new_crs_id = generate_new_crs_id()
            CompanyRequirementSkill.objects.create(
                crs_id=new_crs_id,
                cr=company_req,
                skill=skill,
                skill_type='Hard Skill'
            )

        for skill_id in new_soft_skills:
            skill = Skill.objects.get(skill_id=skill_id)
            new_crs_id = generate_new_crs_id()
            CompanyRequirementSkill.objects.create(
                crs_id=new_crs_id,
                cr=company_req,
                skill=skill,
                skill_type='Soft Skill'
            )

        return redirect('internship_icc')

    return redirect('internship_icc')

def clear_student_cache(request):
    """
    Utility view to clear all cache for current student
    """
    student = _get_student_for_request(request)
    if not student:
        return JsonResponse({'success': False, 'error': 'Student not found'}, status=401)
    
    try:
        ModelCache.invalidate_all(str(student.student_id))
        
        if request.headers.get('Accept') == 'application/json':
            return JsonResponse({
                'success': True,
                'message': 'Cache cleared successfully'
            })
        else:
            messages.success(request, 'Cache cleared successfully')
            return redirect(request.META.get('HTTP_REFERER', 'learning'))
            
    except Exception as e:
        if request.headers.get('Accept') == 'application/json':
            return JsonResponse({
                'success': False,
                'error': str(e)
            }, status=500)
        else:
            messages.error(request, f'Error clearing cache: {e}')
            return redirect(request.META.get('HTTP_REFERER', 'learning'))

def get_student_learning_data(request):
    """
    Comprehensive API to get all student learning data
    """
    student = _get_student_for_request(request)
    if not student:
        return JsonResponse({'success': False, 'error': 'Student not found'}, status=401)
    
    try:
        # Get all learning related data
        recommendations = get_course_recommendations(str(student.student_id)) or []
        skill_gaps = get_skill_gap_for_student_company(str(student.student_id)) or []
        certificates = Certificate.objects.filter(student_id=student.student_id)
        
        # Get student skills
        student_skills = StudentSkill.objects.filter(student_id=student.student_id).first()
        hard_skills = []
        soft_skills = []
        
        if student_skills:
            if student_skills.hard_skill:
                hard_skills = [s.strip() for s in student_skills.hard_skill.split(',') if s.strip()]
            if student_skills.soft_skill:
                soft_skills = [s.strip() for s in student_skills.soft_skill.split(',') if s.strip()]
        
        # Prepare certificates data
        certificates_data = []
        for cert in certificates:
            certificates_data.append({
                'id': cert.pk,
                'name': cert.certificate_name,
                'skill_type': cert.skill_type,
                'skill_name': cert.skill_name,
                'file_url': cert.file.url if cert.file else None
            })
        
        return JsonResponse({
            'success': True,
            'data': {
                'student': {
                    'id': student.student_id,
                    'name': student.full_name,
                    'email': student.email
                },
                'recommendations': recommendations,
                'skill_gaps': skill_gaps,
                'skills': {
                    'hard_skills': hard_skills,
                    'soft_skills': soft_skills
                },
                'certificates': certificates_data,
                'stats': {
                    'total_recommendations': len(recommendations),
                    'high_priority_recommendations': len([r for r in recommendations if r.get('priority') == 'High']),
                    'skill_gaps_count': len(skill_gaps),
                    'certificates_count': len(certificates_data)
                }
            }
        })
        
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=500)