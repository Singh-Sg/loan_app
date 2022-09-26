# -*- coding: utf-8 -*-
# Generated by Django 1.11.12 on 2019-01-22 16:11
from __future__ import unicode_literals

from django.db import migrations
import django_fsm


class Migration(migrations.Migration):

    dependencies = [
        ('loans', '0073_auto_20190118_1401'),
    ]

    operations = [
        migrations.AlterField(
            model_name='disbursement',
            name='state',
            field=django_fsm.FSMField(choices=[('requested', 'Disbursement requested'), ('sent', 'Money disbursed'), ('cancelled', 'Disbursement cancelled')], default='sent', max_length=50),
        ),
    ]
