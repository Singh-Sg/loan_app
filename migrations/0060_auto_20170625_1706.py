# -*- coding: utf-8 -*-
# Generated by Django 1.11.1 on 2017-06-25 17:06
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('loans', '0059_auto_20170625_1307'),
    ]

    operations = [
        migrations.AlterField(
            model_name='disbursement',
            name='method',
            field=models.IntegerField(choices=[(1, 'Cash at agent'), (2, 'KBZ Bank OTC transfer'), (3, 'Wave Peer-to-Peer transfer')], default=1),
        ),
    ]
