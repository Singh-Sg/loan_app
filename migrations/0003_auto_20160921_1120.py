# -*- coding: utf-8 -*-
# Generated by Django 1.10.1 on 2016-09-21 11:20
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('loans', '0002_auto_20160921_1018'),
    ]

    operations = [
        migrations.AlterField(
            model_name='loan',
            name='loan_contract_photo',
            field=models.ImageField(blank=True, upload_to=''),
        ),
    ]