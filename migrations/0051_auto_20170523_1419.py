# -*- coding: utf-8 -*-
# Generated by Django 1.11.1 on 2017-05-23 07:49
from __future__ import unicode_literals

from django.db import migrations, models
import django_fsm


class Migration(migrations.Migration):

    dependencies = [
        ('loans', '0050_auto_20170516_1057'),
    ]

    operations = [
        migrations.AddField(
            model_name='loan',
            name='loan_interest_days',
            field=models.DecimalField(decimal_places=2, default=30, max_digits=4),
        ),
        migrations.AddField(
            model_name='loan',
            name='loan_interest_type',
            field=django_fsm.FSMField(choices=[('declining balance - actual / 360', 'declining balance - actual / 360'), ('declining balance = actual / 365', 'declining balance = actual / 365'), ('declining balance - equal repayments', 'declining balance - equal repayments')], default='declining balance - actual / 360', max_length=50),
        ),
        migrations.AlterField(
            model_name='loan',
            name='loan_interest',
            field=models.DecimalField(decimal_places=2, default=0, help_text=' % interest per loan_interest_days ', max_digits=12),
        ),
    ]
