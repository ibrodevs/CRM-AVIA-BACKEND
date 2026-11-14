import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

pytestmark = pytest.mark.django_db(transaction=True)


def test_legacy_org_strings_migrate_without_granting_roles_or_losing_manager():
    executor = MigrationExecutor(connection)
    executor.migrate([('accounts', '0007_absencerequest')])
    try:
        old = executor.loader.project_state([('accounts', '0007_absencerequest')]).apps
        Org = old.get_model('tenancy', 'Organization')
        User = old.get_model('accounts', 'User')
        Role = old.get_model('accounts', 'Role')
        Assignment = old.get_model('accounts', 'UserRole')
        tenant = Org.objects.create(name='Migration Test', slug='migration-org')
        lead = User.objects.create(tenant=tenant, email='lead-migration@test.local', password='!', status='active')
        user = User.objects.create(tenant=tenant, email='migration@test.local', password='!', status='active', department=' Продажи ', position='Оператор', manager=lead)
        role = Role.objects.create(tenant=tenant, code='operator', name='Оператор')
        Assignment.objects.create(user=user, role=role)
        executor = MigrationExecutor(connection)
        executor.migrate([('accounts', '0010_jobposition_decision_comment_and_more')])
        new = executor.loader.project_state([('accounts', '0010_jobposition_decision_comment_and_more')]).apps
        saved = new.get_model('accounts', 'User').objects.get(pk=user.pk)
        unit = new.get_model('accounts', 'OrgUnit').objects.get(pk=saved.org_unit_id)
        job = new.get_model('accounts', 'JobPosition').objects.get(pk=saved.job_position_id)
        assert unit.name == 'Продажи' and job.name == 'Оператор'
        assert unit.status == job.status == 'approved'
        assert unit.default_role_id is None and job.role_id is None
        assert saved.manager_id == lead.pk
        assert new.get_model('accounts', 'UserRole').objects.filter(user_id=user.pk, role_id=role.pk).exists()
    finally:
        MigrationExecutor(connection).migrate([('accounts', '0010_jobposition_decision_comment_and_more')])
