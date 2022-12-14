# -*- coding: utf-8 -*-
# Generated by Django 1.10.1 on 2016-11-03 07:20
from __future__ import unicode_literals

import datetime
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('loans', '0020_auto_20161030_1505'),
    ]

    operations = [
        migrations.AlterField(
            model_name='loan',
            name='contract_date',
            field=models.DateField(default=datetime.date(2016, 11, 3)),
        ),
        migrations.AlterField(
            model_name='loan',
            name='contract_due_date',
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='loan',
            name='guarantor',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='guarantors', to='borrowers.Borrower'),
        ),
    ]
