# Generated by Django 5.0.9 on 2024-09-19 10:17

from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
    ]

    operations = [
        migrations.CreateModel(
            name='InterfaceTypeMapping',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('librenms_type', models.CharField(max_length=100, unique=True)),
                ('netbox_type', models.CharField(default='other', max_length=50)),
            ],
        ),
    ]
