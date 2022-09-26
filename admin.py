import datetime
from decimal import *
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.forms import ModelForm, ValidationError as FormValidationError
from django.forms.models import BaseInlineFormSet
from reversion.admin import VersionAdmin
from .models import Disbursement, Loan, Notification, PhotoSignature, RepaymentScheduleLine, Repayment, \
    ReasonForDelayedRepayment, SuperUsertoLenderPayment, LOAN_REPAID, DISBURSEMENT_SENT, DISB_METHOD_WAVE_TRANSFER, \
    DISB_METHOD_BANK_TRANSFER, DISB_METHOD_WAVE_N_CASH_OUT, Reconciliation, DefaultPrediction
from borrowers.models import Borrower
from django.forms import DateInput, NumberInput
from django.db import models
from django.contrib import messages
from django.utils.html import format_html
from django.urls import reverse


class RepaymentScheduleLineFormset(BaseInlineFormSet):

    def __init__(self, *args, **kwargs):
        """
        Set the initial values in a RepaymentScheduleLine when created in the admin
        """
        super(RepaymentScheduleLineFormset, self).__init__(*args, **kwargs)
        if self.request.method == 'GET':
            if kwargs['instance'] and kwargs['instance'].normal_repayment_amount is not None and kwargs['instance'].bullet_repayment_amount is not None and RepaymentScheduleLine.objects.filter(loan=kwargs['instance']).count() == 0:
                loan = kwargs['instance']
                self.initial = []
                no_of_lines = Loan.get_number_of_repayments(loan.loan_amount, loan.normal_repayment_amount, loan.bullet_repayment_amount)

                for line_no in range(no_of_lines):
                    # FIXME: this should be granular to a level that makes sense per currency

                    line = {'date': loan.uploaded_at + datetime.timedelta(days=line_no + 1),
                            'principal': loan.bullet_repayment_amount if line_no == no_of_lines - 1 else loan.normal_repayment_amount,
                            'fee': loan.loan_fee if line_no == 0 else 0,
                            'interest': 0,
                            'penalty': 0,
                            }
                    self.initial.append(line)

    def clean(self):
        """
        Validate data input in the admin interface to avoid problems down the line.
        We check for consistency between loan header and the inlines.
        This only applies to changes made through the admin, the API calls do not go through
        this validation.
        """
        if any(self.errors):
            return
        sum_principal = 0
        sum_fee = 0
        sum_subscription = 0
        upload_date = self.forms[0].cleaned_data['loan'].uploaded_at
        for f in self.forms:
            try:
                if not f.cleaned_data['DELETE']:
                    sum_principal += f.cleaned_data['principal']
                    sum_fee += f.cleaned_data['fee']
                    sum_subscription += f.cleaned_data['subscription']
                    summation = f.cleaned_data['principal'] + f.cleaned_data['fee'] + f.cleaned_data['interest'] + f.cleaned_data['penalty'] + f.cleaned_data['subscription']
                    if f.cleaned_data['amount'] != summation:
                        # ideally, None should be used instead of amount so that the error will show as non_field_errors
                        # but, material admin is not showing non_field_errors in formset
                        f.add_error('amount', ValidationError(
                            'amount=[%(amount)s] is not equal to '
                            'principal+fee+interest+penalty=[%(summation)s]',
                            code='unequal_amount',
                            params={'amount': f.cleaned_data['amount'],
                                    'summation': summation
                                    }
                        ))
            except KeyError:
                # in case of empty form
                pass

        # subscription total will be used in RepaymentInlineFormset
        # https://stackoverflow.com/q/13526792/8211573
        self.instance.__subscription_total__ = sum_subscription

        # check that amount matches sum of lines
        loan_amount = self.forms[0].cleaned_data['loan'].loan_amount
        if loan_amount != sum_principal:
            raise ValidationError(
                'The loan amount [%(loan_amount)s] does not match the '
                'sum of principal of all lines [%(sum_principal)s].',
                code='amount_mismatch',
                params={'loan_amount': loan_amount,
                        'sum_principal': sum_principal
                        }
            )
        # Add sum of repayment schedule loan fees in Loan Fee fields.
        loan_fee = self.forms[0].cleaned_data['loan'].loan_fee = sum_fee
        # Check if back office guys saving loan without loan fee
        if loan_fee <= 0:
            loan = self.forms[0].cleaned_data['loan']
            messages.warning(self.request, 'Please add loan "' + str(loan) + '" fee in the repayment schedule lines.')


@admin.register(RepaymentScheduleLine)
class RepaymentScheduleLineAdmin(VersionAdmin, admin.ModelAdmin):

    def has_module_permission(self, request):
        """
        Hide the RepaymentScheduleLine model from the main admin page.
        This does not prevent a direct access via the url :)
        """
        return request.user.is_superuser


class RepaymentScheduleLineInline(admin.TabularInline):
    model = RepaymentScheduleLine
    formset = RepaymentScheduleLineFormset
    ordering = ['date']
    # about resizing field
    # https://stackoverflow.com/questions/910169/resize-fields-in-django-admin
    # about which widget to use for which field
    # https://docs.djangoproject.com/en/1.11/ref/forms/fields/#built-in-field-classes
    # without this override, DateField is too small and inconvenient to read in smaller screens
    formfield_overrides = {
        models.DateField: {'widget': DateInput(attrs={'size': '40em'})},
        models.DecimalField: {'widget': NumberInput(attrs={'size': '10em'})},
    }

    def get_formset(self, request, obj=None, **kwargs):
        formset = super(RepaymentScheduleLineInline, self).get_formset(request, obj, **kwargs)
        formset.request = request
        return formset

    def get_extra(self, request, obj=None, **kwargs):
        extra = super(RepaymentScheduleLineInline, self).get_extra(request, obj, **kwargs)
        if request.method == 'GET':
            # FIXME: for now we use a 2 step process to save a loan schedule:
            # step 1: input borrower, guarantor and number of repayments, save
            # step 2: reopen the same loan, the schedule is now prepopulated (through this method)
            # amend as needed and save
            extra = 0
            if obj is not None and obj.lines.count() == 0:
                extra = Loan.get_number_of_repayments(obj.loan_amount, obj.normal_repayment_amount, obj.bullet_repayment_amount)
        return extra


class RepaymentAdminForm(ModelForm):
    def clean_amount(self):
        if self.cleaned_data['amount'] == 0:
            raise FormValidationError('Repayment amount cannot be 0', code='zero_amount')
        return self.cleaned_data['amount']


class RepaymentInlineFormset(BaseInlineFormSet):
    """
    Formset for the inline repayments in loan admin view
    """

    def clean(self):
        """
        Prevent some common errors when inputting loan repayment info, and raise ValidationError
        so the user can better understand what to do about it.
        """
        # calculate the totals in repayments and scream if it's too much
        sum_repayments = 0
        sum_subscription = 0
        sum_principal = 0

        # check that we are not trying to repay more than loan amount/fee/subscription
        # if there are any repayments
        if len(self.forms) > 0:
            for f in self.forms:
                if not f.cleaned_data['DELETE']:
                    sum_subscription += f.cleaned_data['subscription']
            total_to_repay = self.forms[0].cleaned_data['loan'].loan_amount + \
                self.forms[0].cleaned_data['loan'].loan_fee
            if sum_repayments > total_to_repay:
                raise ValidationError(
                    'You are trying to repay [%(sum_repayments)s] total, '
                    'which is more than the loan amount+fee [%(total_to_repay)s].',
                    code='repayment_too_high',
                    params={'sum_repayments': sum_repayments,
                            'total_to_repay': total_to_repay
                            }
                )

            if sum_subscription > self.instance.__subscription_total__:
                raise ValidationError(
                    'You are trying to repay [%(sum_subscription)s] total, '
                    'which is more than subscription total [%(subscription_total)s].',
                    code='subscription_too_high',
                    params={'sum_subscription': sum_subscription,
                            'subscription_total': self.instance.__subscription_total__
                            }
                )

        # repayments of already repaid loan can't be modify directly
        if len(self.forms) > 0:
            loan = self.forms[0].cleaned_data['loan']
            if loan.state == LOAN_REPAID or loan.repaid_on is not None:
                raise ValidationError(
                    'You cannot modify a fully repaid loan. '
                    'Please change the state to "Loan disbursed" and '
                    'clear the "repaid on" date before saving again',
                    code='modifying_already_repaid_loan',
                )

        # To prevent changes when updating repayments if repayment's principal amount exceeds loan amount
        if len(self.forms) > 0:
            loan_amount = self.forms[0].cleaned_data['loan'].loan_amount
            for f in self.forms:
                if not f.cleaned_data['DELETE'] and f.cleaned_data['id']:
                    sum_principal += f.cleaned_data['principal']

            po = loan_amount - sum_principal
            if po < 0:
                raise ValidationError(
                    'You are trying to pay [%(sum_principal)s] total, '
                    'which is more than the loan amount [%(loan_amount)s].',
                    code='repayment_too_high',
                    params={'sum_principal': sum_principal,
                            'loan_amount': loan_amount
                            }
                )


class RepaymentInline(admin.TabularInline):
    form = RepaymentAdminForm
    formset = RepaymentInlineFormset
    model = Repayment
    ordering = ['date']
    fields = ('date', 'amount', 'fee', 'penalty', 'interest', 'principal', 'subscription', 'reason_for_delay', 'link')
    readonly_fields = ('link',)
    extra = 0
    # about resizing field
    # https://stackoverflow.com/questions/910169/resize-fields-in-django-admin
    # about which widget to use for which field
    # https://docs.djangoproject.com/en/1.11/ref/forms/fields/#built-in-field-classes
    # without this override, DateField is too small and inconvenient to read in smaller screens
    formfield_overrides = {
        models.DateField: {'widget': DateInput(attrs={'size': '40em'})},
        models.DecimalField: {'widget': NumberInput(attrs={'size': '10em'})},
    }

    # show admin link
    # using show_change_link=True option is better but it is not working with material admin
    # ref: https://stackoverflow.com/a/15055057/8211573
    def link(self, instance):
        url = reverse('admin:%s_%s_change' % (instance._meta.app_label,
                                              instance._meta.model_name),
                      args=(instance.id,))
        return format_html(u'<a href="{}" target="_blank">edit</a>', url)


@admin.register(Loan)
class LoanAdmin(VersionAdmin, admin.ModelAdmin):
    inlines = [RepaymentScheduleLineInline, RepaymentInline]
    readonly_fields = ('loan_fee', 'uploaded_at', 'days_late', 'total_outstanding', 'passed_credit')

    def contract_num(self, obj):
        """temp fix for empty_value_display not working (somehow)"""
        return ("%s" % obj.contract_number if obj.contract_number else "<none>")
    contract_num.short_description = 'Contract number'

    def loan_agent(self, obj):
        """show the agent in the list of loans"""
        return obj.borrower.agent

    list_display = ('borrower', 'loan_agent', 'uploaded_at', 'loan_amount', 'contract_due_date', 'repaid_on', 'passed_credit')
    list_filter = ('borrower__name_en', 'loan_amount', 'borrower__agent__name', )

    exclude = (
        'number_of_repayments',
    )

    def passed_credit(self, obj):
        """show the agent in the list of loans"""
        try:
            data = obj.default_prediction.all()
            if data:
                return data[0].passed_credit
            return None
        except Exception as e:
            return None

    def lookup_allowed(self, key, value):
        """
        Django has a security feature preventing custom lookups from random urls.
        This allows a specific one we need for the error report showing borrowers with
        multiple open loans.
        See http://stackoverflow.com/a/6468224/1138710 for inspiration :)
        """
        if key in ('borrower__borrower_number', ):
            return True
        return super(LoanAdmin, self).lookup_allowed(key, value)

    # redefine add_view and change_view to create loans in 2 steps:
    # 1- loan amount, and repayment details (normal amount, bullet)
    # 2- everything else
    def add_view(self, request, form_url='', extra_context=None):
        self.fields = ('borrower', 'guarantor', 'loan_amount', 'normal_repayment_amount', 'bullet_repayment_amount', )
        self.inlines = []
        self.save_as_continue = True
        return super(LoanAdmin, self).add_view(request, form_url, extra_context)

    def change_view(self, request, object_id, form_url='', extra_context=None):
        self.fields = None
        self.inlines = [RepaymentScheduleLineInline, RepaymentInline]

        # allows copying the form to a new object
        # /ref/contrib/admin/index.html#django.contrib.admin.ModelAdmin.save_as
        # FIXME: not supported by django-material yet?
        self.save_as = True

        return super(LoanAdmin, self).change_view(request, object_id, form_url, extra_context)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        """sort borrowers by name when picking one for a loan contract in the admin"""
        if db_field.name in ['borrower', 'guarantor']:
            kwargs["queryset"] = Borrower.objects.order_by('name_en')
        return super(LoanAdmin, self).formfield_for_foreignkey(db_field, request, **kwargs)


@admin.register(Repayment)
class RepaymentAdmin(VersionAdmin, admin.ModelAdmin):
    list_display = ('pk', 'date', 'amount', 'loan', 'repayment_agent')
    list_filter = ('date', 'loan__borrower', 'loan__loan_amount', 'loan__borrower__agent', 'reconciliation_status')
    readonly_fields = ('uploaded_at',)

    def repayment_agent(self, obj):
        """show the agent in the list display"""
        return obj.loan.borrower.agent


class ReasonForDelayedRepaymentAdmin(admin.ModelAdmin):
    list_display = ('pk', 'reason_en', 'reason_mm')


class PhotoSignatureAdmin(admin.ModelAdmin):
    list_display = ('loan', 'timestamp', 'is_valid',)
    readonly_fields = ('borrower_profile_photo_tag', 'signature_photo_tag', )

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        """Sort keys to Loan by borrower name+loan amount to help finding them"""
        if db_field.name == 'loan':
            kwargs['queryset'] = Loan.objects.order_by('borrower__name_en', 'loan_amount')
        return super(PhotoSignatureAdmin, self).formfield_for_foreignkey(db_field, request, **kwargs)


class NotificationAdmin(admin.ModelAdmin):
    list_display = ('loan', 'new_state')


# class DisbursementAdminForm(ModelForm):
#     def clean(self):
#         """
#         prevent some common errors when inputting disbursement info
#         for now, validations are only for state DISBURSEMENT_SENT
#         """
#         if self.cleaned_data['state'] == DISBURSEMENT_SENT:
#             if self.cleaned_data['transfer'] is None:
#                 self.add_error('transfer', ValidationError(
#                     'Transfer is missing',
#                     code='transfer missing',
#                 ))

#             if self.cleaned_data['method'] in {DISB_METHOD_WAVE_TRANSFER, DISB_METHOD_BANK_TRANSFER, DISB_METHOD_WAVE_N_CASH_OUT}:
#                 if self.cleaned_data['provider_transaction_id'] is None:
#                     self.add_error('provider_transaction_id', ValidationError(
#                         'Transaction id missing',
#                         code='transaction id missing',
#                     ))


class DisbursementAdmin(admin.ModelAdmin):
    list_display = ('method', 'timestamp', 'disbursed_to', 'amount',)
    # form = DisbursementAdminForm


class SuperUsertoLenderPaymentAdmin(admin.ModelAdmin):
    list_display = ('super_user', 'date', 'method', 'amount', 'success',)
    list_filter = ('super_user', 'transfer__timestamp', 'transfer__method', 'transfer__amount', 'transfer__transfer_successful',)
    ordering = ['-transfer__timestamp']

    def method(self, obj):
        return obj.transfer.get_method_display()

    def date(self, obj):
        return obj.transfer.timestamp.strftime("%d %b")

    def amount(self, obj):
        return obj.transfer.amount

    def success(self, obj):
        return obj.transfer.transfer_successful


class ReconciliationRepaymentInline(admin.TabularInline):
    model = Repayment
    readonly_fields = ('id', 'date', 'amount', 'super_user', 'loan_link',)
    fields = ('id', 'date', 'amount', 'super_user', 'loan_link',)
    show_change_link = True  # this is not working with material admin
    ordering = ['date']
    extra = 0

    def super_user(self, obj):
        return obj.loan.borrower.agent.name

    # show loan link
    # ref: https://stackoverflow.com/a/15055057/8211573
    def loan_link(self, instance):
        url = reverse('admin:%s_%s_change' % (instance._meta.app_label,
                                              instance.loan._meta.model_name),
                      args=(instance.loan.id,))
        return format_html(u'<a href="{}" target="_blank">loan</a>', url)


class ReconciliationSuperUsertoLenderInline(admin.TabularInline):
    model = SuperUsertoLenderPayment
    readonly_fields = ('id', 'date', 'amount', 'super_user', 'link')
    fields = ('id', 'date', 'amount', 'super_user', 'link')
    show_change_link = True  # this is not working with material admin
    extra = 0

    def date(self, obj):
        return obj.transfer.timestamp.strftime("%b. %d, %Y")

    def amount(self, obj):
        return obj.transfer.amount

    # show admin link
    # using show_change_link=True option is better but it is not working with material admin
    # ref: https://stackoverflow.com/a/15055057/8211573
    def link(self, instance):
        url = reverse('admin:%s_%s_change' % (instance._meta.app_label,
                                              instance._meta.model_name),
                      args=(instance.id,))
        return format_html(u'<a href="{}" target="_blank">edit</a>', url)


class ReconciliationAdmin(admin.ModelAdmin):
    list_display = ('super_user', 'reconciled_at',)
    inlines = [ReconciliationRepaymentInline, ReconciliationSuperUsertoLenderInline]
    ordering = ['-reconciled_at']

    def super_user(self, obj):
        su = "None"
        if obj.superusertolenderpayment_set.count() > 0:
            su = obj.superusertolenderpayment_set.first().super_user.name
        return su


class DefaultPredictionAdmin(admin.ModelAdmin):
    list_display = ('borrower', 'loan_agent', 'uploaded_at', 'loan_amount', 'passed_credit')

    def borrower(self, obj):
        return obj.loan.borrower

    def loan_agent(self, obj):
        return obj.loan.borrower.agent.name

    def uploaded_at(self, obj):
        return obj.loan.uploaded_at

    def loan_amount(self, obj):
        return obj.loan.loan_amount


admin.site.register(Reconciliation, ReconciliationAdmin)
admin.site.register(Disbursement, DisbursementAdmin)
# admin.site.register(Loan, LoanAdmin)
admin.site.register(Notification, NotificationAdmin)
# admin.site.register(RepaymentScheduleLine, RepaymentScheduleLineAdmin)
# admin.site.register(Repayment, RepaymentAdmin)
admin.site.register(ReasonForDelayedRepayment, ReasonForDelayedRepaymentAdmin)
admin.site.register(PhotoSignature, PhotoSignatureAdmin)
admin.site.register(SuperUsertoLenderPayment, SuperUsertoLenderPaymentAdmin)
admin.site.register(DefaultPrediction, DefaultPredictionAdmin)
