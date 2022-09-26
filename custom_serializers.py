from django.contrib.auth.models import User

from rest_framework import serializers
from drf_extra_fields.fields import Base64ImageField

from org.models import FieldOfficer
from loans.models import Repayment
from borrowers.models import Agent, Borrower
from borrowers.serializers import MarketSerializer, BorrowerSummarySerializer


class AgentSerializerVersion2(serializers.ModelSerializer):
    market = MarketSerializer()

    class Meta:
        model = Agent
        fields = ('id', 'name', 'phone_number', 'market')


class FieldOfficerSerializer(serializers.ModelSerializer):

    class Meta:
        model = FieldOfficer
        fields = '__all__'


class UserSerializer(serializers.ModelSerializer):

    class Meta:
        model = User
        fields = ['id', 'username']


class AgentFullProfileSerializer(serializers.ModelSerializer):
    market = MarketSerializer()
    field_officer = FieldOfficerSerializer()
    user = UserSerializer()
    borrowers = serializers.SerializerMethodField()

    class Meta:
        model = Agent
        fields = '__all__'

    def get_borrowers(self, obj, pk=None):
        """
        return a set of borrowers
        """
        borrower_list = []
        for b in Borrower.objects.filter(agent_id=obj.pk):
            borrower_list.append(BorrowerSummarySerializer(b).data)
        return borrower_list


class GuarantorSerializerVersion2(serializers.ModelSerializer):
    borrower_photo = Base64ImageField(required=False)

    class Meta:
        model = Borrower
        fields = ('id', 'name_en', 'borrower_photo')


class BorrowerSerializerVersion2(serializers.ModelSerializer):
    name_en = serializers.CharField(allow_blank=True, max_length=100)
    name_mm = serializers.CharField(allow_blank=True, max_length=100)
    borrower_photo = Base64ImageField(required=False)
    id_photo_front = Base64ImageField(required=False)
    id_photo_back = Base64ImageField(required=False)
    agent = AgentSerializerVersion2()

    class Meta:
        model = Borrower
        fields = ['id', 'name_en', 'name_mm', 'phone_number_mpt', 'phone_number_ooredoo', 'id_photo_front', 'id_photo_back', 'borrower_photo', 'comments', 'agent']

    def get_field_names(self, declared_fields, info):
        expanded_fields = super(BorrowerSerializerVersion2, self).get_field_names(declared_fields, info)

        if getattr(self.Meta, 'extra_fields', None):
            return expanded_fields + self.Meta.extra_fields
        else:
            return expanded_fields


class RepaymentFullSerializer(serializers.ModelSerializer):
    recorded_by = UserSerializer()

    class Meta:
        model = Repayment
        fields = '__all__'
