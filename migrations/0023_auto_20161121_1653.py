# -*- coding: utf-8 -*-
# Generated by Django 1.10.1 on 2016-11-21 16:53
from __future__ import unicode_literals

import datetime
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('loans', '0022_auto_20161116_0526'),
    ]

    operations = [
        migrations.AlterField(
            model_name='loan',
            name='contract_date',
            field=models.DateField(default=datetime.date(2016, 11, 21)),
        ),
    ]
