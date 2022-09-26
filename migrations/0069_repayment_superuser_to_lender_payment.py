# -*- coding: utf-8 -*-
# Generated by Django 1.11.5 on 2018-04-16 05:16
from __future__ import unicode_literals

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('loans', '0068_auto_20180204_1637'),
    ]

    operations = [
        migrations.AddField(
            model_name='repayment',
            name='superuser_to_lender_payment',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, to='loans.SuperUsertoLenderPayment'),
        ),
    ]