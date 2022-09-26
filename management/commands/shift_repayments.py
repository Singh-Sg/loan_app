from django.core.management.base import BaseCommand, CommandError
from django.utils.text import slugify
from faker import Faker
from datetime import date, datetime, timedelta
from borrowers.models import Borrower, Agent, Market, Relative
from loans.models import Loan, RepaymentScheduleLine


class Command(BaseCommand):
    """
    Shift all repayments scheduled for a defined period.
    Can be used for holiday seasons when borrowers won't repay any loans.
    This does not affect actual repayments, only the schedule.
    """
    help = 'Shift all planned repayments by the specified number of days. Can only be used for dates in the future.'

    def date_string(self, string):
        return datetime.strptime(string, '%Y-%m-%d')

    def add_arguments(self, parser):
                parser.add_argument('start_date', help='the first day to shift the payments from, in YYYY-MM-DD format', type=self.date_string)
                parser.add_argument('end_date', help='the last day to shift the payments, in YYYY-MM-DD format', type=self.date_string)
                parser.add_argument('shift_days', help='the number of days to shift by', type=int)

    def handle(self, *args, **options):
        start_date = options['start_date']
        end_date = options['end_date']
        shift_days = options['shift_days']

        # find the repayment lines that are impacted by the shift
        lines_to_shift = RepaymentScheduleLine.objects.filter(
            date__gte=start_date
        ).filter(
            date__lte=end_date
        )
        # we need to shift all repayment lines for the loans impacted by the shift
        # otherwise we would get 2 repayments for a few days after the shift period
        loans_impacted = set()  # use a set to avoid duplicate loan IDs
        for l in lines_to_shift:
            loans_impacted.add(l.loan.id)
        self.stdout.write('Found {} loan(s) to amend.'.format(len(loans_impacted)))

        for l in loans_impacted:
            loan = Loan.objects.get(pk=l)
            lines = loan.lines.filter(date__gte=start_date)
            self.stdout.write('Moving {} line(s) for loan #{}.'.format(lines.count(), l))
            for line in lines:
                line.date = line.date + timedelta(days=shift_days)
                line.save()
        self.stdout.write('Done')
