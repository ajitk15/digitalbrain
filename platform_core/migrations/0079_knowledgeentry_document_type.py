from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('platform_core', '0078_merge_source_quality_serviceops'),
    ]

    operations = [
        migrations.AddField(
            model_name='knowledgeentry',
            name='document_type',
            field=models.CharField(blank=True, max_length=40),
        ),
    ]
