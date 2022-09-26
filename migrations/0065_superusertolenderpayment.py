# -*- coding: utf-8 -*-
# Generated by Django 1.11.5 on 2017-11-24 11:45
from __future__ import unicode_literals

from django.db import migrations, models
import django.db.models.deletion
import loans.models


class Migration(migrations.Migration):

    dependencies = [
        ('org', '0006_auto_20170516_1928'),
        ('borrowers', '0024_auto_20171110_1443'),
        ('payments', '0006_transfer'),
        ('loans', '0064_auto_20170819_0926'),
    ]

    operations = [
        migrations.CreateModel(
            name='SuperUsertoLenderPayment',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('lender', models.ForeignKey(default=loans.models.default_mfi_branch, on_delete=django.db.models.deletion.PROTECT, to='org.MFIBranch')),
                ('super_user', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='borrowers.Agent')),
                ('transfer', models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, to='payments.Transfer')),
            ],
        ),
    ]