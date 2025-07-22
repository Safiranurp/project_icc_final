from django.shortcuts import render, redirect
from django.db import connection
from django.contrib import messages
from .models import Student, Skill, StudentSkill, Certificate
from django.http import JsonResponse

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
    return render(request, 'skill_analysis/student.html')

def student_icc_view(request):
    return render(request, 'skill_analysis/student_icc.html')

def student_data_view(request):
    return render(request, 'skill_analysis/student_data.html')

def learning_view(request):
    return render(request, 'skill_analysis/learning.html')

def skill_icc_view(request):
    return render(request, 'skill_analysis/skill_icc.html')

def internship_view(request):
    return render(request, 'skill_analysis/internship.html')

def internship_icc_view(request):
    return render(request, 'skill_analysis/internship_icc.html')

def internship_form_view(request):
    return render(request, 'skill_analysis/internship_form.html')

def internship_desc_view(request):
    return render(request, 'skill_analysis/internship_desc.html')

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