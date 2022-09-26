# -*- coding: utf-8 -*-
# Generated by Django 1.10.6 on 2017-05-16 04:27
from __future__ import unicode_literals

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('loans', '0049_merge_20170515_1540'),
    ]

    operations = [
        migrations.AlterField(
            model_name='repayment',
            name='reconciliation',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.PROTECT, to='loans.Reconciliation'),
        ),
    ]
