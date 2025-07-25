# Generated by Django 5.2.4 on 2025-07-23 18:18

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('skill_analysis', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='CompanyRequirement',
            fields=[
                ('cr_id', models.TextField(primary_key=True, serialize=False)),
                ('company_name', models.TextField(blank=True, null=True)),
                ('position', models.TextField(blank=True, null=True)),
                ('job_desc', models.TextField(blank=True, null=True)),
            ],
            options={
                'db_table': 'company_requirement',
                'managed': False,
            },
        ),
        migrations.CreateModel(
            name='CompanyRequirementSkill',
            fields=[
                ('crs_id', models.TextField(primary_key=True, serialize=False)),
                ('skill_type', models.TextField(blank=True, null=True)),
            ],
            options={
                'db_table': 'company_requirement_skill',
                'managed': False,
            },
        ),
        migrations.CreateModel(
            name='Course',
            fields=[
                ('course_id', models.IntegerField(primary_key=True, serialize=False)),
                ('subject', models.TextField(blank=True, null=True)),
                ('major', models.IntegerField(blank=True, null=True)),
                ('curriculum', models.TextField(blank=True, null=True)),
                ('sks', models.IntegerField(blank=True, null=True)),
                ('concentration', models.TextField(blank=True, null=True)),
                ('type', models.TextField(blank=True, null=True)),
                ('semester', models.IntegerField(blank=True, null=True)),
            ],
            options={
                'db_table': 'course',
                'managed': False,
            },
        ),
        migrations.CreateModel(
            name='Enrollment',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('subject', models.TextField(blank=True, null=True)),
                ('grade', models.TextField(blank=True, null=True)),
                ('semester', models.IntegerField(blank=True, null=True)),
            ],
            options={
                'db_table': 'enrollment',
                'managed': False,
            },
        ),
        migrations.CreateModel(
            name='SkillMap',
            fields=[
                ('sm_id', models.CharField(max_length=50, primary_key=True, serialize=False)),
                ('course_name', models.TextField(blank=True, null=True)),
                ('hard_skill', models.TextField(blank=True, null=True)),
            ],
            options={
                'db_table': 'skill_map',
                'managed': False,
            },
        ),
        migrations.CreateModel(
            name='StudentCompanyChoice',
            fields=[
                ('id', models.AutoField(primary_key=True, serialize=False)),
                ('position', models.TextField(blank=True, null=True)),
                ('created_at', models.DateTimeField(blank=True, null=True)),
            ],
            options={
                'db_table': 'student_company_choice',
                'managed': False,
            },
        ),
    ]
