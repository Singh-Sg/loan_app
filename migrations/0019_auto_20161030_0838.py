# -*- coding: utf-8 -*-
# Generated by Django 1.10.1 on 2016-10-30 08:38
from __future__ import unicode_literals

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('loans', '0018_loan_expect_sales_growth'),
    ]

    operations = [
        migrations.AlterField(
            model_name='loan',
            name='borrower',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='borrowers', to='borrowers.Borrower'),
        ),
        migrations.AlterField(
            model_name='loan',
            name='guarantor',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='guarantors', to='borrowers.Borrower'),
        ),
        migrations.AlterField(
            model_name='loan',
            name='loan_currency',
            field=models.ForeignKey(default=1, on_delete=django.db.models.deletion.PROTECT, to='zw_utils.Currency'),
        ),
        migrations.AlterField(
            model_name='loan',
            name='purpose',
            field=models.ForeignKey(blank=True, default=None, null=True, on_delete=django.db.models.deletion.PROTECT, to='loans.LoanPurpose'),
        ),
        migrations.AlterField(
            model_name='repayment',
            name='loan',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='loans.Loan'),
        ),
    ]