# -*- coding: utf-8 -*-
# Generated by Django 1.10.3 on 2016-12-08 09:12
from __future__ import unicode_literals

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('loans', '0025_remove_loan_contract_due_date'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='loan',
            name='fee_outstanding',
        ),
        migrations.RemoveField(
            model_name='loan',
            name='interest_outstanding',
        ),
        migrations.RemoveField(
            model_name='loan',
            name='penalty_outstanding',
        ),
        migrations.RemoveField(
            model_name='loan',
            name='principal_outstanding',
        ),
        migrations.AlterField(
            model_name='repayment',
            name='loan',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='repayments', to='loans.Loan'),
        ),
    ]