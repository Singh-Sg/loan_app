# -*- coding: utf-8 -*-
# Generated by Django 1.11.1 on 2017-05-23 08:39
from __future__ import unicode_literals

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('loans', '0051_auto_20170523_1419'),
    ]

    operations = [
        migrations.RenameField(
            model_name='loan',
            old_name='loan_interest',
            new_name='loan_interest_rate',
        ),
    ]
