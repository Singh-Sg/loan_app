# -*- coding: utf-8 -*-
# Generated by Django 1.10.1 on 2016-09-21 09:15
from __future__ import unicode_literals

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('borrowers', '0002_auto_20160921_0908'),
    ]

    operations = [
        migrations.CreateModel(
            name='Loan',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('contract_number', models.CharField(max_length=50)),
                ('contract_date', models.DateField()),
                ('loan_amount', models.DecimalField(decimal_places=2, max_digits=12)),
                ('loan_interest', models.DecimalField(decimal_places=2, max_digits=12)),
                ('loan_fee', models.DecimalField(decimal_places=2, max_digits=12)),
                ('late_penalty_fee', models.DecimalField(decimal_places=2, max_digits=12)),
                ('late_penalty_per_x_days', models.IntegerField()),
                ('late_penalty_max_days', models.IntegerField()),
                ('early_penalty', models.DecimalField(decimal_places=2, max_digits=12)),
                ('loan_contract_photo', models.ImageField(upload_to='')),
                ('contract_due_date', models.DateField()),
                ('contract_length', models.DurationField()),
                ('effective_interest_rate', models.DecimalField(decimal_places=2, max_digits=5)),
                ('pilot', models.CharField(max_length=20)),
                ('borrower', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='borrowers', to='borrowers.Borrower')),
                ('guarantor', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='guarantors', to='borrowers.Borrower')),
            ],
        ),
    ]
