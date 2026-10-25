"""Learning candidates (`learning.py`), with the tenant policy every table with a
required application carries (migration 0091's `secure`)."""

import importlib
import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

ROW_SECURITY = importlib.import_module("platform_core.migrations.0091_row_security")
#: Read by `test_tenancy` with 0091's `TABLES`.
SECURED = ["platform_core_learningcandidate"]


def forwards(apps, schema_editor):
    ROW_SECURITY.secure(schema_editor, SECURED)


def backwards(apps, schema_editor):
    ROW_SECURITY.unsecure(schema_editor, SECURED)


class Migration(migrations.Migration):

    dependencies = [
        ('platform_core', '0103_release_check'),
    ]

    operations = [
        migrations.CreateModel(
            name='LearningCandidate',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('kind', models.CharField(choices=[('change', 'Change record'), ('incident', 'Incident learning'), ('gap', 'Unanswered question'), ('wrong', 'Answer marked wrong')], max_length=10)),
                ('state', models.CharField(choices=[('open', 'Waiting'), ('accepted', 'Accepted'), ('rejected', 'Rejected'), ('withdrawn', 'Withdrawn')], db_default='open', default='open', max_length=10)),
                ('title', models.CharField(max_length=300)),
                ('body', models.TextField(blank=True)),
                ('question', models.CharField(blank=True, max_length=500)),
                ('note', models.CharField(blank=True, max_length=1000)),
                ('expected', models.CharField(blank=True, max_length=200)),
                ('decided_at', models.DateTimeField(blank=True, null=True)),
                ('decision_note', models.CharField(blank=True, max_length=1000)),
                ('signal_at', models.DateTimeField()),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('application', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='platform_core.application')),
                ('benchmark', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='platform_core.benchmarkquestion')),
                ('decided_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='learning_decisions', to=settings.AUTH_USER_MODEL)),
                ('entry', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='platform_core.knowledgeentry')),
                ('message', models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='learning', to='platform_core.chatmessage')),
                ('reported_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='answer_reports', to=settings.AUTH_USER_MODEL)),
                ('run', models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='learning', to='platform_core.factoryrun')),
                ('verdict', models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='learning', to='platform_core.triageverdict')),
            ],
            options={
                'ordering': ['-signal_at', '-id'],
                'indexes': [models.Index(fields=['application', 'state', 'signal_at'], name='platform_co_applica_0c31d2_idx')],
            },
        ),
        migrations.RunPython(forwards, backwards, elidable=False),
    ]
