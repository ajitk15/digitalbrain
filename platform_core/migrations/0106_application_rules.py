"""Who marked a source as rules (`rules.py`). Nullable, so the release before can
still write a source."""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('platform_core', '0105_automatic_publishing'),
    ]

    operations = [
        migrations.AddField(
            model_name='knowledgeentry',
            name='rules_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='knowledgeentry',
            name='rules_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL),
        ),
    ]
