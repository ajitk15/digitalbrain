from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("platform_core", "0116_chat_search_purpose"),
    ]

    operations = [
        migrations.AddField(
            model_name="aiusage",
            name="turns",
            field=models.PositiveIntegerField(db_default=1, default=1),
        ),
        migrations.AddField(
            model_name="agentmeasurement",
            name="runtime",
            field=models.CharField(
                blank=True,
                choices=[
                    ("cli", "Claude CLI"),
                    ("messages", "Messages API"),
                    ("messages-cached", "Messages API, prompt cache"),
                    ("openai", "OpenAI"),
                ],
                db_default="",
                default="",
                max_length=16,
            ),
        ),
    ]
