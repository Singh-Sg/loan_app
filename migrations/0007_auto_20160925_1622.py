# -*- coding: utf-8 -*-
# Generated by Django 1.10.1 on 2016-09-25 16:22
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('loans', '0006_loan_number_of_repayments'),
    ]

    operations = [
        migrations.AlterField(
            model_name='loan',
            name='contract_number',
            field=models.CharField(blank=True, max_length=50),
        ),
    ]
