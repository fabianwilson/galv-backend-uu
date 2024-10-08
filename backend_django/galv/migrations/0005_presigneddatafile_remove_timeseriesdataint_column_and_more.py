# Generated by Django 5.0.2 on 2024-04-02 15:39

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("galv", "0004_harvester_last_check_in_job"),
    ]

    operations = [
        migrations.CreateModel(
            name="PresignedDataFile",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("modified", models.DateTimeField(auto_now=True)),
                (
                    "file",
                    models.FileField(
                        blank=True, null=True, unique=True, upload_to="data"
                    ),
                ),
                ("auth_key", models.TextField(blank=True, null=True, unique=True)),
            ],
            options={
                "abstract": False,
            },
        ),
        migrations.RemoveField(
            model_name="timeseriesdataint",
            name="column",
        ),
        migrations.RemoveField(
            model_name="timeseriesdatastr",
            name="column",
        ),
        migrations.AddField(
            model_name="observedfile",
            name="num_partitions",
            field=models.PositiveIntegerField(
                help_text="Number of partitions in the file's parquet format", null=True
            ),
        ),
        migrations.AddField(
            model_name="observedfile",
            name="storage_urls",
            field=models.JSONField(
                default=list, help_text="URLs for the file's storage"
            ),
        ),
        migrations.DeleteModel(
            name="TimeseriesDataFloat",
        ),
        migrations.DeleteModel(
            name="TimeseriesDataInt",
        ),
        migrations.DeleteModel(
            name="TimeseriesDataStr",
        ),
    ]
