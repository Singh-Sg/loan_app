import logging
from collections import OrderedDict
from django.db import transaction
from drf_extra_fields.fields import Base64ImageField
from rest_framework import serializers
from rest_framework.fields import DateField, DateTimeField, DecimalField
from loans.models import Disbursement, Loan, LoanAlreadyRepaidError, LoanPurpose, LOAN_REQUEST_DRAFT, \
    LOAN_REQUEST_SUBMITTED, Notification, Reconciliation, PhotoSignature, RepaymentScheduleLine, Repayment, RepaymentTooBigError, \
    ReasonForDelayedRepayment, WaveTransferDisbursement, BankTransferDisbursement, WaveTransferAndCashOutDisbursement, \
    DISB_METHOD_WAVE_TRANSFER, DISB_METHOD_BANK_TRANSFER, DISB_METHOD_WAVE_N_CASH_OUT, DISBURSEMENT_SENT, LoanRequestReview, SuperUsertoLenderPayment
# import phonenumbers as pn
from payments.models import Transfer
from loans.custom_serializers import BorrowerSerializerVersion2, GuarantorSerializerVersion2
from borrowers.models import Agent, Borrower


class RepaymentScheduleLineSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(required=False)

    class Meta:
        model = RepaymentScheduleLine
        fields = ('id', 'date', 'principal', 'fee', 'interest', 'penalty',)

    def to_representation(self, instance):
        """
        Override the generic code in DRF.serializers. This is about twice as fast as original code.
        """
        ret = OrderedDict()
        ret['id'] = instance.id
        ret['date'] = DateField().to_representation(instance.date)
        df = DecimalField(decimal_places=2, max_digits=12)
        ret['principal'] = df.to_representation(instance.principal)
        ret['fee'] = df.to_representation(instance.fee)
        ret['interest'] = df.to_representation(instance.interest)
        ret['penalty'] = df.to_representation(instance.penalty)
        return ret


class ReasonForDelayedRepaymentSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReasonForDelayedRepayment
        fields = ('id', 'reason_en', 'reason_mm')


class RepaymentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Repayment
        # also update .to_representation() if you modify the list of fields below
        fields = ('id', 'date', 'amount', 'recorded_at', 'loan', 'recorded_by', 'reason_for_delay', 'reconciliation_status')

    def create(self, validated_data):
        """
        Overload the default method so we can catch exceptions raised
        in model.save() and turn them into useful info for the user.
        FIXME: This exception should probably be raised in the validation to do things
        by the book but...
        """
        try:
            # force the agent to be the logged in user
            validated_data['recorded_by'] = self.context['request'].user
            r = Repayment.objects.create(**validated_data)
        except RepaymentTooBigError as e:
            # convert this into an error that DRF will send to the client
            # instead of just dying with an error 500
            raise serializers.ValidationError({
                'message': e.message,
                'error': 'repayment_too_big',
                'max_repayable': e.params['max_repayable'],
            })
        except LoanAlreadyRepaidError as e:
            raise serializers.ValidationError({
                'message': e.message,
                'error': 'loan_already_repaid',
            })
        return r

    def to_representation(self, instance):
        """
        Override the generic method in DRF to speed up rendering to JSON.
        About twice as fast as original code.
        """
        ret = OrderedDict()
        ret['id'] = instance.id
        ret['date'] = DateField().to_representation(instance.date)
        ret['amount'] = DecimalField(decimal_places=2, max_digits=12).to_representation(instance.amount)
        ret['recorded_at'] = DateTimeField().to_representation(instance.recorded_at)
        ret['loan'] = instance.loan_id
        ret['recorded_by'] = instance.recorded_by_id
        ret['reason_for_delay'] = instance.reason_for_delay_id
        ret['reconciliation_status'] = instance.reconciliation_status
        return ret


class NewRepaymentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Repayment
        # also update .to_representation() if you modify the list of fields below
        fields = ('id', 'date', 'amount', 'recorded_at', 'loan', 'recorded_by', 'reason_for_delay', 'reconciliation_status', 'note')

    def to_representation(self, instance):
        """
        Override the generic method in DRF to speed up rendering to JSON.
        About twice as fast as original code.
        """
        ret = OrderedDict()
        ret['id'] = instance.id
        ret['date'] = DateField().to_representation(instance.date)
        ret['amount'] = DecimalField(decimal_places=2, max_digits=12).to_representation(instance.amount)
        ret['recorded_at'] = DateTimeField().to_representation(instance.recorded_at)
        ret['loan'] = instance.loan_id
        ret['recorded_by'] = instance.recorded_by_id
        ret['reason_for_delay'] = instance.reason_for_delay_id
        ret['reconciliation_status'] = instance.reconciliation_status
        ret['borrower_name'] = instance.loan.borrower.name_en
        ret['borrower_id'] = instance.loan.borrower.id
        ret['note'] = instance.note
        return ret


class LoanPurposeSerializer(serializers.ModelSerializer):
    class Meta:
        model = LoanPurpose
        fields = '__all__'


class LoanSerializer(serializers.ModelSerializer):
    lines = RepaymentScheduleLineSerializer(many=True)
    repayments = RepaymentSerializer(many=True)
    purpose = serializers.PrimaryKeyRelatedField(queryset=LoanPurpose.objects.all(), required=False, allow_null=True)
    loan_contract_photo = Base64ImageField(required=False)

    class Meta:
        model = Loan
        fields = '__all__'
        read_only_fields = ('repaid_on', 'repayments',)

    def create(self, validated_data):
        """
        create() is called on POST to the loan URL.
        Create the Loan and its related RepaymentScheduleLine children.
        """
        with transaction.atomic():
            lines = validated_data.pop('lines')
            validated_data.pop('repayments')
            # pop repayments to prevent Django from trying to find related ones and crashing
            if validated_data['state'] == LOAN_REQUEST_DRAFT:
                validated_data['state'] = LOAN_REQUEST_SUBMITTED
            loan = Loan.objects.create(**validated_data)
            # TODO: transition DRAFT loans to SUBMITTED?
            for line in lines:
                RepaymentScheduleLine.objects.create(loan=loan, **line)
        return loan

    def validate(self, data):
        """
        Verify that the loan header data matches the lines
        """
        try:
            # verify sum of line.principal is equal to loan_amount
            sum_of_principal = sum(l['principal'] for l in data['lines'])
            if sum_of_principal != data['loan_amount']:
                raise serializers.ValidationError('Sum of lines principal must equal loan_amount')
            # same job for fees
            sum_of_fee = sum(l['fee'] for l in data['lines'])
            if sum_of_fee != data['loan_fee']:
                raise serializers.ValidationError('Sum of lines fee must equal loan_amount')
        except KeyError:
            # if any of the keys above doesn't exist, we'll get a KeyError
            raise serializers.ValidationError('Malformed loan object')

        return data


class LoanPUTSerializer(LoanSerializer):
    class Meta:
        model = Loan
        # exclude = ('repayments',)
        fields = "__all__"

    def validate(self, data):
        # FIXME: this is broken, we need a proper validation here !
        return data

    def update(self, instance, validated_data):
        """
        PUT requests MUST include all loan lines or the request will be rejected (HTTP 400 error).
        The old lines will be updated where possible (if the PKs match), dropped ones will be deleted from the database
        and new ones saved instead.
        """
        # remove any repayments just in case
        validated_data.pop('repayments', [])
        request_lines = validated_data.pop('lines', [])
        if len(request_lines) == 0:
            raise serializers.ValidationError('Invalid PUT request')

        # make sure we don't leave the DB half updated
        with transaction.atomic():
            loan = super(LoanPUTSerializer, self).update(instance, validated_data)
            new_lines = [l for l in request_lines if l.get('id', None) is None]
            updated_lines = [l for l in request_lines if l.get('id', None) is not None]
            updated_ids = [l.get('id', None) for l in updated_lines]

            # delete old lines that are not in the request
            for l in instance.lines.all():
                if l.id not in updated_ids:
                    l.delete()

            # update old lines that are in the request
            for l in updated_lines:
                line = RepaymentScheduleLine.objects.get(pk=l.get('id'))
                for k in l.keys():
                    setattr(line, k, l.get(k))
                line.save()

            # save new lines
            for l in new_lines:
                RepaymentScheduleLine.objects.create(loan=instance, **l)

        return loan


class LoanPATCHSerializer(LoanSerializer):
    def update(self, instance, validated_data):
        """
        PATCH requests CANNOT include loan lines, they are made to update the main body of the loan only.
        """
        validated_data.pop('repayments', [])
        if len(validated_data['lines']) > 0:
            raise serializers.ValidationError('Invalid PATCH request')

        validated_data.pop('lines', [])

        return super(LoanPATCHSerializer, self).update(instance, validated_data)

    def validate(self, data):
        # FIXME: this is broken, we need a proper validation here !
        return data


class LoanSummarySerializer(serializers.ModelSerializer):
    class Meta:
        model = Loan
        # provide both contract_date and uploaded_at to the app, so both the old and new version get what they need
        # TODO: once app production-v36 is not in use anymore, remove contract_date from here
        fields = ('id', 'loan_amount', 'contract_date', 'uploaded_at', 'next_disbursement_date', 'days_late', 'next_loan_max_amount', 'repaid_on', 'state',)


class LoanRequestReviewSerializer(serializers.ModelSerializer):
    class Meta:
        model = LoanRequestReview
        fields = ('id', 'loan', 'approved', 'comments', 'alternative_offer',)

    def create(self, validated_data):
        """
        overrides create to set reviewer
        """
        validated_data['reviewer'] = self.context['request'].user
        return LoanRequestReview.objects.create(**validated_data)


class NotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Notification
        fields = ('loan', 'new_state',)


class PhotoSignatureSerializer(serializers.ModelSerializer):
    photo = Base64ImageField(required=False)

    class Meta:
        model = PhotoSignature
        fields = ('loan', 'photo', 'timestamp',)


class DisbursementSerializer(serializers.ModelSerializer):
    details = serializers.JSONField()

    class Meta:
        model = Disbursement
        fields = ('id', 'provider_transaction_id', 'method', 'amount', 'disbursed_to', 'fees_paid', 'details', 'loans_disbursed')

    def change_state(self, disb):
        disb.state = DISBURSEMENT_SENT
        disb.save()

    def create(self, validated_data):
        """
        choose proxy child class according to method
        """
        # getting request.user from serializer
        # https://stackoverflow.com/a/30203950/8211573
        # disbursed_by = None
        # request = self.context.get("request")
        # if request and hasattr(request, "user"):
        #     disbursed_by = request.user
        # this field do not exist in Disbursement model
        loans_disbursed = validated_data.pop('loans_disbursed')

        disbursement_methods = {
            DISB_METHOD_WAVE_TRANSFER: WaveTransferDisbursement,
            DISB_METHOD_BANK_TRANSFER: BankTransferDisbursement,
            DISB_METHOD_WAVE_N_CASH_OUT: WaveTransferAndCashOutDisbursement,
        }

        with transaction.atomic():
            disb = disbursement_methods[validated_data['method']].objects.create(**validated_data)
            # set disbursement foreignkey of loans of loans_disbursed to this disbursement
            for loan in loans_disbursed:
                loan.disbursement = disb
                loan.save()
        self.change_state(disb)
        return disb

    def validate(self, data):
        """
        Validate phone numbers if they are Myanmar phone numbers or in the case of disbursement by Wave
        also check if number is Telenor. Phone numbers are only included in details field
        """
        # PHONE_NUMBER_MISSING = "Phone Number Missing"
        # PHONE_NUMBER_INVALID = "Phone Number Invalid"

        # this function do nothing. this is just for consistency
        def method_wave_validator(input_data):
            return input_data

        def method_bank_validator(input_data):
            # try:
            #     ph_no = pn.parse(input_data['details']['recipient_phone_number'], "MM")
            # except KeyError:
            #     raise serializers.ValidationError(PHONE_NUMBER_MISSING)

            # if pn.is_valid_number_for_region(ph_no, "MM"):
            #     return input_data
            # else:
            #     raise serializers.ValidationError(PHONE_NUMBER_INVALID)
            return input_data

        def method_wave_and_cashout_validator(input_data):
            # try:
            #     ph_no_1 = pn.parse(input_data['details']['recipient_number'], "MM")
            #     ph_no_2 = pn.parse(input_data['details']['sender_number'], "MM")
            # except KeyError:
            #     raise serializers.ValidationError(PHONE_NUMBER_MISSING)

            # check_if_valid = pn.is_valid_number_for_region(ph_no_1, "MM") and pn.is_valid_number_for_region(ph_no_2, "MM")
            # # Telenor start with 97
            # check_if_telenor = (str(ph_no_1.national_number)[:2] == '97') and (str(ph_no_2.national_number)[:2] == '97')

            # if check_if_valid and check_if_telenor:
            #     return input_data
            # else:
            #     raise serializers.ValidationError(PHONE_NUMBER_INVALID)
            return input_data

        method_validators = {
            DISB_METHOD_WAVE_TRANSFER: method_wave_validator,
            DISB_METHOD_BANK_TRANSFER: method_bank_validator,
            DISB_METHOD_WAVE_N_CASH_OUT: method_wave_and_cashout_validator,
        }

        try:
            validated_data = method_validators[data['method']](data)
            return validated_data
        except KeyError:
            raise serializers.ValidationError("Invalid Method")


class DisbursementDisburseSerializer(DisbursementSerializer):
    """
    this serializer is to be used with DisbursementViewset
    disburse API endpoint
    """
    class Meta:
        model = Disbursement
        fields = ('provider_transaction_id',)

    def validate(self, data):
        """
        check if transfer and transaction id exist
        """
        # FIXME: this test may need to be changed for the case that transfer and transaction id are already included when creating disbursement
        # if 'transfer' not in data:
        #     raise serializers.ValidationError('Transfer Missing')

        # if self.instance.method:
        #     if 'provider_transaction_id' not in data:
        #         raise serializers.ValidationError('Transaction Id Missing')

        return data


class SuperUsertoLenderPaymentCustomSerializer(serializers.Serializer):
    # FIXME: set min_length=1
    repayments = serializers.ListField()
    amount = serializers.DecimalField(decimal_places=2, max_digits=12)
    method = serializers.ChoiceField(choices=((1, 'Cash in hand'), (2, 'Wave to Wave'), (3, 'Paysbuy'), (4, 'KBZ Bank (OTC)'), (5, 'Pay via Wave API')),)
    details = serializers.JSONField(style={'base_template': 'textarea.html'})

    def repaymentValidation(self, repayments, *args):
        invalidId = []
        final_list = []
        duplicateRepId = set()
        for r in repayments:
            try:
                Repayment.objects.get(id=r)
            except Exception:
                invalidId.append(r)
            if r not in final_list:
                final_list.append(r)
            else:
                duplicateRepId.add(r)

        return {'invalid': invalidId, 'duplicate': duplicateRepId}

    def log(self, message, repayments, *args):
        logger = logging.getLogger('root')
        logger.error(message, extra={
            'repayments': repayments,
        })

    def validate(self, data):
        """
        1) Find duplicate repayments.
        2) Find invalid repayments.
        3) Maintain log
        """
        repayments = data.get('repayments')
        if not repayments:
            raise serializers.ValidationError({'success': False, 'error': 'Please provide repayments'})
        repayment = self.repaymentValidation(repayments)
        invalidRepayments = repayment.get('invalid')
        duplicateRepayments = repayment.get('duplicate')

        if invalidRepayments and duplicateRepayments:
            self.log(message='Duplicate and Invalid Repayments found', repayments=repayment)
            raise serializers.ValidationError({'success': False, 'error': 'Duplicate and Invalid Repayments found', 'repayments': repayment})

        if invalidRepayments:
            self.log(message='Repayments not found', repayments=invalidRepayments)
            raise serializers.ValidationError({'success': False, 'error': 'Given Repayments is invalid', 'repayments': invalidRepayments})

        if duplicateRepayments:
            self.log(message='Repayments duplicate found', repayments=duplicateRepayments)
            raise serializers.ValidationError({'success': False, 'error': 'Duplicate Repayments found', 'repayments': duplicateRepayments})
        return data


class ReconciliationPOSTSerializer(serializers.Serializer):
    repayments = serializers.ListField(min_length=1)
    su2lpayments = serializers.ListField(min_length=1)

    def validate(self, data):
        repayments = Repayment.objects.filter(id__in=data['repayments'])
        su2lpayments = SuperUsertoLenderPayment.objects.filter(id__in=data['su2lpayments'])

        # check if all data are from same SuperUser
        su_repayments = set(repayments.values_list('loan__borrower__agent', flat=True))
        su_su2lpayments = set(su2lpayments.values_list('super_user', flat=True))
        superusers = set.union(su_repayments, su_su2lpayments)
        if len(superusers) != 1:
            raise serializers.ValidationError({'error': 'Those data are linked to more than one SuperUser'})

        # check if amount is equal
        total_repayments = sum(repayments.values_list('amount', flat=True))
        total_su2lpayments = sum(su2lpayments.values_list('transfer__amount', flat=True))
        if total_repayments != total_su2lpayments:
            raise serializers.ValidationError({'error': 'Total of Repayment and total of SuperUsertoLenderPayment are not equal'})

        return data


class TransferSerializer(serializers.ModelSerializer):
    method = serializers.CharField(source='get_method_display')

    class Meta:
        model = Transfer
        fields = ('id', 'timestamp', 'amount', 'method', 'note')


class BorrowerForReconSerializer(serializers.ModelSerializer):
    class Meta:
        model = Borrower
        fields = ('id', 'borrower_number', 'name_en', 'name_mm')


class Loan2Serializer(serializers.ModelSerializer):
    borrower = BorrowerForReconSerializer()

    class Meta:
        model = Loan
        fields = ('id', 'borrower')


class RepaymentForReconSerializer(serializers.ModelSerializer):
    loan = Loan2Serializer()

    class Meta:
        model = Repayment
        fields = ('id', 'loan')


class ReconciliationSerializer(serializers.ModelSerializer):
    repayment_list = RepaymentForReconSerializer(many=True, read_only=True)

    class Meta:
        model = Reconciliation
        fields = ('id', 'reconciled_by', 'repayment_list')


class SuperUsertoLenderPaymentSerializer(serializers.ModelSerializer):
    transfer = TransferSerializer()
    reconciliation = ReconciliationSerializer()

    class Meta:
        model = SuperUsertoLenderPayment
        fields = ('id', 'super_user', 'transfer', 'reconciliation_status', 'reconciliation')


class TransferFullSerializer(serializers.ModelSerializer):
    method = serializers.CharField(source='get_method_display')

    class Meta:
        model = Transfer
        fields = ('id', 'timestamp', 'amount', 'method', 'details', 'note')


class SuperuserSerializer(serializers.ModelSerializer):
    class Meta:
        model = Agent
        fields = '__all__'


class SuperUsertoLenderFullPaymentSerializer(serializers.ModelSerializer):
    transfer = TransferFullSerializer()
    super_user = SuperuserSerializer()

    class Meta:
        model = SuperUsertoLenderPayment
        fields = ('id', 'super_user', 'transfer', 'reconciliation_status')


class ReasonForDelaySerializer(serializers.ModelSerializer):

    class Meta:
        model = ReasonForDelayedRepayment
        fields = '__all__'


class RepaymentSerializerVersion2(serializers.ModelSerializer):
    reason_for_delay = ReasonForDelaySerializer()
    superuser_to_lender_payment = SuperUsertoLenderPaymentSerializer()

    class Meta:
        model = Repayment
        fields = ('id', 'date', 'amount', 'principal', 'fee', 'interest', 'penalty', 'subscription', 'reason_for_delay', 'superuser_to_lender_payment')


class LoanSerializerFullDetail(serializers.ModelSerializer):
    lines = RepaymentScheduleLineSerializer(many=True)
    repayments = RepaymentSerializerVersion2(many=True)
    purpose = serializers.PrimaryKeyRelatedField(queryset=LoanPurpose.objects.all(), required=False, allow_null=True)
    loan_contract_photo = Base64ImageField(required=False)
    borrower = BorrowerSerializerVersion2()
    guarantor = GuarantorSerializerVersion2()
    signatures = serializers.SerializerMethodField()
    totaloutstanding = serializers.DecimalField(max_digits=12, decimal_places=2, source='total_outstanding')
    loan_currency = serializers.CharField(source='loan_currency.name_en')

    class Meta:
        model = Loan
        fields = '__all__'
        read_only_fields = ('repaid_on', 'repayments',)

    def get_signatures(self, obj):
        sign = PhotoSignature.objects.filter(loan_id=obj.pk)
        return PhotoSignatureSerializer(sign, many=True).data


class NoteSerializer(serializers.Serializer):
    """
    Note serializer to add note for diffrent objects.
      1) Repayments
      2) Repayments Schedule Line
      3) Transfer
      4) Agent
      5) Borrwer
    """
    repayments = serializers.CharField(required=False)
    lines = serializers.CharField(required=False)
    transfer = serializers.CharField(required=False)
    agent = serializers.CharField(required=False)
    borrower = serializers.CharField(required=False)
    note = serializers.CharField(required=True)

    def repaymentValidation(self, repayment, *args):
        invalidId = []
        try:
            Repayment.objects.get(id=repayment)
        except Exception:
            invalidId.append(repayment)
        return {'invalid': invalidId}

    def linesValidation(self, lines, *args):
        invalidId = []
        try:
            RepaymentScheduleLine.objects.get(id=lines)
        except Exception:
            invalidId.append(lines)
        return {'invalid': invalidId}

    def transferValidation(self, transfer, *args):
        invalidId = []
        try:
            Transfer.objects.get(id=transfer)
        except Exception:
            invalidId.append(transfer)
        return {'invalid': invalidId}

    def agentValidation(self, agent, *args):
        invalidId = []
        try:
            Agent.objects.get(id=agent)
        except Exception:
            invalidId.append(agent)
        return {'invalid': invalidId}

    def borrowerValidation(self, borrower, *args):
        invalidId = []
        try:
            Borrower.objects.get(id=borrower)
        except Exception:
            invalidId.append(borrower)
        return {'invalid': invalidId}

    def log(self, message, object_id, *args):
        logger = logging.getLogger('root')
        logger.error(message, extra={
            'object_id': object_id,
        })

    def validate(self, data):
        """
        1) Find invalid id.
        2) Maintain log
        """
        repayments = data.get('repayments')
        if repayments:
            repayment = self.repaymentValidation(repayments)
            invalidRepayments = repayment.get('invalid')
            if invalidRepayments:
                self.log(message='Repayments not found', object_id=invalidRepayments)
                raise serializers.ValidationError({'success': False, 'error': 'Given Repayments is invalid', 'repayments': invalidRepayments})

        lines = data.get('lines')
        if lines:
            lines = self.linesValidation(lines)
            invalidLines = lines.get('invalid')
            if invalidLines:
                self.log(message='Repayments Schedule Lines not found', object_id=invalidLines)
                raise serializers.ValidationError({'success': False, 'error': 'Given Repayments Schedule Lines is invalid', 'lines': invalidLines})

        transfer = data.get('transfer')
        if transfer:
            transfer = self.transferValidation(transfer)
            invalidTransfer = transfer.get('invalid')
            if invalidTransfer:
                self.log(message='Transfer not found', object_id=invalidTransfer)
                raise serializers.ValidationError({'success': False, 'error': 'Given transfer is invalid', 'transfer': invalidTransfer})

        agent = data.get('agent')
        if agent:
            agent = self.agentValidation(agent)
            invalidAgent = agent.get('invalid')
            if invalidAgent:
                self.log(message='Agent not found', object_id=invalidAgent)
                raise serializers.ValidationError({'success': False, 'error': 'Given Agent is invalid', 'agent': invalidAgent})

        borrower = data.get('borrower')
        if borrower:
            borrower = self.borrowerValidation(borrower)
            invalidBorrower = borrower.get('invalid')
            if invalidBorrower:
                self.log(message='Borrower not found', object_id=invalidBorrower)
                raise serializers.ValidationError({'success': False, 'error': 'Given Borrower is invalid', 'repayments': invalidBorrower})

        return data


class CashTransferSerializer(serializers.Serializer):
    """
    Add transfer by Admin User
    """
    timestamp = serializers.DateTimeField(format="%Y-%m-%dT%H:%M:%S", required=True)
    amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    agent = serializers.IntegerField(required=True)
    note = serializers.CharField(required=False)

    def validate(self, data):
        """
        """
        agent = data.get('agent')
        try:
            Agent.objects.get(id=agent)
        except Exception:
            raise serializers.ValidationError({'success': False, 'error': 'Given Agent is invalid'})
        return data
