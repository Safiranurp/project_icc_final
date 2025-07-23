from django.urls import path
from . import views
from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
    path('', views.login_view, name='login'),
    path('home_student/', views.home_student_view, name='home_student'),
    path('home_icc/', views.home_icc_view, name='home_icc'),
    path('analysis/', views.course_view, name='analysis'),
    path('course/', views.course_view, name='course'),
    path('skill/', views.skill_view, name='skill'),
    path('skill_icc/', views.skill_icc_view, name='skill_icc'),
    path('student/', views.student_view, name='student'),
    path('update-profile-photo/', views.update_profile_photo, name='update_profile_photo'),
    path('student_icc/', views.student_icc_view, name='student_icc'),
    path('student_data/<str:student_id>/', views.student_data_view, name='student_data'),
    path('learning/', views.learning_view, name='learning'),
    path('internship/', views.internship_view, name='internship'),
    path('internship_form/', views.internship_form_view, name='internship_form'),
    path('internship_icc/', views.internship_icc_view, name='internship_icc'),
    path('internship_desc/<str:company_name>/', views.internship_desc_view, name='internship_desc'),
    path('add_skill/', views.add_skill, name='add_skill'),
    path('get_skills_by_type/', views.get_skills_by_type, name='get_skills_by_type'),
    path('add_certificate/', views.add_certificate, name='add_certificate'),
    path('delete_skill/<str:skill_name>/', views.delete_skill, name='delete_skill'),
    path('delete_certificate/<int:c_id>/', views.delete_certificate, name='delete_certificate'),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
