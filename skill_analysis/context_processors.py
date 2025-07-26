from .models import Student

def student_context(request):
    student_name = None
    student_id = request.session.get('student_id')
    if student_id:
        try:
            student = Student.objects.get(student_id=student_id)
            student_name = student.full_name  # atau .name kalau field-nya bernama seperti itu
        except Student.DoesNotExist:
            pass
    return {
        'student_name': student_name
    }
