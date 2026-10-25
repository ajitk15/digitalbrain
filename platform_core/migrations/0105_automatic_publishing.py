"""Who let a version publish itself (`release.auto_publish`). Both columns are
nullable or carry a database default, so the release before can still write."""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('platform_core', '0104_learning_candidates'),
    ]

    operations = [
        migrations.AddField(
            model_name='knowledgegraph',
            name='auto_publish_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='knowledgegraph',
            name='auto_publish_note',
            field=models.CharField(blank=True, db_default='', default='', max_length=300),
        ),
    ]
