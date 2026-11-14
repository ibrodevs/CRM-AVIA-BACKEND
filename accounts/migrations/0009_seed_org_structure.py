from django.db import migrations


def seed(apps, schema_editor):
    User = apps.get_model('accounts', 'User')
    Unit = apps.get_model('accounts', 'OrgUnit')
    Position = apps.get_model('accounts', 'JobPosition')
    for user in User.objects.all().iterator():
        unit = None
        department = ' '.join(user.department.split())
        position = ' '.join(user.position.split())
        if department:
            unit, _ = Unit.objects.get_or_create(tenant_id=user.tenant_id, name_key=department.casefold(), defaults={'name': department, 'status': 'approved'})
        job = None
        if position:
            job, _ = Position.objects.get_or_create(tenant_id=user.tenant_id, unit=unit, name_key=position.casefold(), defaults={'name': position, 'status': 'approved'})
        # Do not infer permission roles or a reporting line from free-text titles.
        User.objects.filter(pk=user.pk).update(org_unit=unit, job_position=job)


class Migration(migrations.Migration):
    dependencies = [('accounts', '0008_jobposition_user_job_position_orgunit_and_more')]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
