# -*- coding: utf-8 -*-
# Generated by Django 1.10.6 on 2017-04-28 08:28
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('loans', '0042_auto_20170316_1357'),
    ]

    operations = [
        migrations.AddField(
            model_name='repayment',
            name='reconcile_status',
            field=models.BooleanField(default=False),
        ),
    ]
