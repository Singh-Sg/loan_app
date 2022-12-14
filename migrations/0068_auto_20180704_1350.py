# -*- coding: utf-8 -*-
# Generated by Django 1.11.12 on 2018-07-04 13:50
from __future__ import unicode_literals

from decimal import Decimal
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('loans', '0067_superusertolenderpayment_loans'),
    ]

    operations = [
        migrations.AddField(
            model_name='repayment',
            name='subscription',
            field=models.DecimalField(decimal_places=2, default=0, max_digits=12),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='repaymentscheduleline',
            name='subscription',
            field=models.DecimalField(decimal_places=2, default=Decimal('0'), max_digits=12),
        ),
    ]
