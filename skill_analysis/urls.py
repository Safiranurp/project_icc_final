from django.urls import path
from . import views
from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
    path('', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('home_student/', views.home_student_view, name='home_student'),
    path('home_icc/', views.home_icc_view, name='home_icc'),
    path('analysis/', views.analysis_view, name='analysis'),
    path('course/', views.course_view, name='course'),
    path('skill/', views.skill_view, name='skill'),
    path('add_skill/', views.add_skill, name='add_skill'),
    path('student/', views.student_view, name='student'),
    path('update-profile-photo/', views.update_profile_photo, name='update_profile_photo'),
    path('student_icc/', views.student_icc_view, name='student_icc'),
    path('student_data/<str:student_id>/', views.student_data_view, name='student_data'),
    path('learning/', views.learning_view, name='learning'),
    path('internship/', views.internship_view, name='internship'),
    path('internship_desc/<str:company_name>/', views.internship_desc_view, name='internship_desc'),
    path('get_skills_by_type/', views.get_skills_by_type, name='get_skills_by_type'),
    path('add_certificate/', views.add_certificate, name='add_certificate'),
    path('delete_skill/<str:skill_name>/', views.delete_skill, name='delete_skill'),
    path('delete_certificate/<int:c_id>/', views.delete_certificate, name='delete_certificate'),
    path('internships_icc/', views.showIntern, name='internship_icc'),
    path('internships_form', views.skill_internForm, name='intern_form'),
    path('internships/submit_intern/', views.submit_intern, name='submit_intern'),
    path('internships/delete/<str:cr_id>/', views.delete_intern, name='delete_intern'),
    path('internship/edit/<str:cr_id>/', views.edit_intern, name='edit_intern'),
    path('internships/update/<str:cr_id>/', views.update_intern, name='update_intern'),
    path('skill_icc/', views.skill_list, name='skill_icc'),
    path('add_skill_icc/', views.add_skill_icc, name='add_skill_icc'),

] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
