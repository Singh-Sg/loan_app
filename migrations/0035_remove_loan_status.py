# -*- coding: utf-8 -*-
# Generated by Django 1.10.5 on 2017-03-05 12:09
from __future__ import unicode_literals

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('loans', '0034_loan_status'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='loan',
            name='status',
        ),
    ]