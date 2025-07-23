import os
from django.db import models
from django.utils.timezone import now

from django.db import models

class Student(models.Model):
    student_id = models.CharField(primary_key=True, max_length=50)
    full_name = models.TextField(blank=True, null=True)
    batch = models.IntegerField(blank=True, null=True)
    current_status = models.TextField(blank=True, null=True)
    transfer = models.BooleanField(blank=True, null=True)
    program_session = models.TextField(blank=True, null=True)
    sem_start_date = models.DateField(blank=True, null=True)
    sem_end_date = models.DateField(blank=True, null=True)
    year_start = models.IntegerField(blank=True, null=True)
    year_end = models.IntegerField(blank=True, null=True)
    gpa = models.DecimalField(max_digits=4, decimal_places=2, blank=True, null=True)
    email = models.TextField(blank=True, null=True)
    image = models.ImageField(upload_to='student_photos/', blank=True, null=True)

    class Meta:
        managed = False
        db_table = 'student'

    @property
    def major(self):
        return "Information System"


class Course(models.Model):
    course_id = models.IntegerField(primary_key=True)
    subject = models.TextField(blank=True, null=True)
    major = models.IntegerField(blank=True, null=True)
    curriculum = models.TextField(blank=True, null=True)
    sks = models.IntegerField(blank=True, null=True)
    concentration = models.TextField(blank=True, null=True)
    type = models.TextField(blank=True, null=True)
    semester = models.IntegerField(blank=True, null=True)

    class Meta:
        managed = False
        db_table = 'course'


class Enrollment(models.Model):
    student = models.ForeignKey(Student, on_delete=models.DO_NOTHING, db_column='student_id')
    course = models.ForeignKey(Course, on_delete=models.DO_NOTHING, db_column='course_id')
    subject = models.TextField(blank=True, null=True)
    grade = models.TextField(blank=True, null=True)
    semester = models.IntegerField(blank=True, null=True)

    class Meta:
        managed = False
        db_table = 'enrollment'


class Skill(models.Model):
    skill_id = models.CharField(primary_key=True, max_length=50)
    skill_name = models.TextField(blank=True, null=True)
    skill_type = models.TextField(blank=True, null=True)

    class Meta:
        managed = False
        db_table = 'skill'
    
    def __str__(self):
        return f"{self.skill_type} ({self.skill_id})"


class SkillMap(models.Model):
    sm_id = models.CharField(primary_key=True, max_length=50)
    course = models.ForeignKey(Course, on_delete=models.DO_NOTHING, db_column='course_id', blank=True, null=True)
    course_name = models.TextField(blank=True, null=True)
    hard_skill = models.TextField(blank=True, null=True)

    class Meta:
        managed = False
        db_table = 'skill_map'


def certificate_upload_path(instance, filename):
    return os.path.join(f'student_{instance.student.student_id}', filename)


class Certificate(models.Model):
    c_id = models.AutoField(primary_key=True)
    student = models.ForeignKey(Student, on_delete=models.CASCADE)
    skill_type = models.CharField(max_length=50)  
    skill_name = models.CharField(max_length=100)
    certificate_name = models.CharField(max_length=255)
    file = models.FileField(upload_to=certificate_upload_path)
    date_uploaded = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.student.full_name} - {self.certificate_name}"

    class Meta:
        managed = False
        db_table = 'certificate'


class StudentSkill(models.Model):
    ss_id = models.AutoField(primary_key=True)
    student = models.ForeignKey(Student, on_delete=models.DO_NOTHING, db_column='student_id')
    hard_skill = models.CharField(max_length=100, blank=True, null=True)
    soft_skill = models.CharField(max_length=100, blank=True, null=True)

    class Meta:
        managed = False
        db_table = 'studentskill'


class CompanyRequirement(models.Model):
    cr_id = models.TextField(primary_key=True)
    company_name = models.TextField(blank=True, null=True)
    position = models.TextField(blank=True, null=True)
    job_desc = models.TextField(blank=True, null=True)

    class Meta:
        managed = False
        db_table = 'company_requirement'
    
    def __str__(self):
        return f"{self.company_name} - {self.position}"


class CompanyRequirementSkill(models.Model):
    crs_id = models.TextField(primary_key=True)
    cr = models.ForeignKey(CompanyRequirement, on_delete=models.DO_NOTHING, db_column='cr_id', blank=True, null=True)
    skill = models.ForeignKey(Skill, on_delete=models.DO_NOTHING, db_column='skill_id', blank=True, null=True)
    skill_type = models.TextField(blank=True, null=True)

    class Meta:
        managed = False
        db_table = 'company_requirement_skill'

    def __str__(self):
        return f"{self.cr_id} needs ({self.skill_type})"


class StudentCompanyChoice(models.Model):
    id = models.AutoField(primary_key=True)
    student = models.ForeignKey(Student, on_delete=models.DO_NOTHING, db_column='student_id')
    company = models.ForeignKey(CompanyRequirement, on_delete=models.DO_NOTHING, db_column='company_id', blank=True, null=True)
    position = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        managed = False
        db_table = 'student_company_choice'
    
    def __str__(self):
        return f'{self.student_name} -> {self.company_name}'