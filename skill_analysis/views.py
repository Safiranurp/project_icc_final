from django.shortcuts import render, redirect, get_object_or_404
from django.db import connection
from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Q
from .models import Student, Skill, StudentSkill, Certificate, CompanyRequirement, CompanyRequirementSkill, StudentCompanyChoice
from django.http import JsonResponse
import os
import json
from django.utils.timezone import now
from django.conf import settings
from django.core.files.storage import FileSystemStorage

def home_student_view(request):
    if 'student_id' not in request.session:
        return redirect('login')
    return render(request, 'skill_analysis/home_student.html')

def home_icc_view(request):
    if 'student_id' not in request.session:
        return redirect('login')
    return render(request, 'skill_analysis/home_icc.html')

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

    return render(request, 'skill_analysis/student.html', {
        'student': student,
        'hard_skills': hard_skills,
        'soft_skills': soft_skills,
        'company_name': company_name,
        'position': position,
        'skill_fulfilled_percent': skill_fulfilled,
        'skill_not_fulfilled_percent': skill_not_fulfilled,
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

    # Ambil data pilihan perusahaan dan posisi
    internship = StudentCompanyChoice.objects.filter(student_id=student_id).first()
    company_name = "-"
    position = "-"

    if internship and internship.company_id:
        company = CompanyRequirement.objects.filter(cr_id=internship.company_id).first()
        if company:
            company_name = company.company_name or "-"
            position = company.position or "-"

    # Pisahkan skill
    hard_skills = []
    soft_skills = []

    for skill in student_skills:
        if skill.hard_skill:
            hard_skills.extend([s.strip() for s in skill.hard_skill.split(',') if s.strip()])
        if skill.soft_skill:
            soft_skills.extend([s.strip() for s in skill.soft_skill.split(',') if s.strip()])

    # Hitung Skill Process
    skill_fulfilled = calculate_skill_process(student_id)
    skill_not_fulfilled = round(100 - skill_fulfilled, 2)

    return render(request, 'skill_analysis/student_data.html', {
        'student': student,
        'hard_skills': hard_skills,
        'soft_skills': soft_skills,
        'company_name': company_name,
        'position': position,
        'skill_fulfilled_percent': skill_fulfilled,
        'skill_not_fulfilled_percent': skill_not_fulfilled,
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

    # Ambil skill student
    skills = StudentSkill.objects.filter(student_id=student_id)
    student_hard = set()
    student_soft = set()

    for s in skills:
        if s.hard_skill:
            student_hard.update([x.strip().lower() for x in s.hard_skill.split(',')])
        if s.soft_skill:
            student_soft.update([x.strip().lower() for x in s.soft_skill.split(',')])

    # Ambil skill dari sertifikat
    cert_skills = set(Certificate.objects.filter(student_id=student_id)
                      .values_list('skill_name', flat=True))
    cert_skills = set(x.strip().lower() for x in cert_skills if x)

    # Hitung bobot
    score = 0
    bonus = 0
    max_score = len(required_skills)  # hanya skill yang dihitung

    for skill in required_skills:
        if skill in student_hard or skill in student_soft:
            score += 1  # core skill
            if skill in cert_skills:
                bonus += 0.5  # sertifikat hanya bonus

    final_score = score + bonus
    max_total_score = max_score + (0.5 * len(required_skills))  # jika semua skill punya sertifikat

    return round((final_score / max_total_score) * 100, 2)


def learning_view(request):
    return render(request, 'skill_analysis/learning.html')

def skill_icc_view(request):
    return render(request, 'skill_analysis/skill_icc.html')

def internship_view(request):
    companies = CompanyRequirement.objects.values_list('company_name', flat=True).distinct()
    return render(request, 'skill_analysis/internship.html', {'companies': companies})


def internship_icc_view(request):
    return render(request, 'skill_analysis/internship_icc.html')

def internship_form_view(request):
    return render(request, 'skill_analysis/internship_form.html')

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

        if request.method == "POST":
            student_id= request.session.get('student_id')
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
            
            return redirect('apply', company_name=company_name)
        
        except Exception as e:
            messages.error(request, f"Failed to save your choice: {str(e)}")
            return redirect('apply', company_name=company_name)
        
    context = {
        'company_name': company_name,
        'positions': list(positions_data.keys()),
        'positions_data': json.dumps(positions_data)
    }
    return render(request, 'skill_analysis/internship_desc.html', context)

def anaysis_view(request):
    return render(request, 'skill_analysis/analysis.html')

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
    
from django.db.models import Q

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

def logout_view(request):
    request.session.flush()
    return redirect('login')