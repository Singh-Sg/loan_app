# -*- coding: utf-8 -*-
# Generated by Django 1.10.3 on 2016-12-20 16:30
from __future__ import unicode_literals

from django.db import migrations, models
import loans.models


class Migration(migrations.Migration):

    dependencies = [
        ('loans', '0027_auto_20161215_0446'),
    ]

    operations = [
        migrations.AlterField(
            model_name='loan',
            name='repaid_on',
            field=models.DateField(blank=True, help_text='Automatically set to the date of the repayment closing the loan. Not set for open loans.', null=True, validators=[loans.models.Loan.validate_repaid_on_not_in_future]),
        ),
    ]
