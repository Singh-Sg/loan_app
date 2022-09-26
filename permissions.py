from rest_framework import permissions

"""
Custom permissions for Loan related API views
"""


class LoanViewPermissions(permissions.BasePermission):
    """
    Custom permissions that applies to LoanViewSet.
    .has_permission is called first, and .has_object_permission is only
    called if the first one returns True
    """
    def has_permission(self, request, view):
        """
        Check if the user is allowed to access the Loan view
        """
        if request.method in permissions.SAFE_METHODS:
            # GET, OPTIONS, HEAD
            return True
        elif request.method == 'POST':
            if view.action == 'approve_loan':
                return request.user.has_perm('loans.zw_approve_loan')
            # trying to create a loan
            return request.user.has_perm('loans.zw_request_loan')
        elif request.method in ['PATCH', 'PUT']:
            # modifying the loan object
            # TODO: we might need finer grained sorting here
            # to distinguish what actions are attempted
            # approve/sign/disburse...
            return request.user.has_perm('loans.change_loan')
        elif request.method == 'DELETE':
            return request.user.has_perm('loans.delete_loan')
        else:
            # we don't know what this is, reject it
            return False

    def has_object_permission(self, request, view, obj):
        """
        This runs after `has_permission` if it returned True.
        Checks if the user has access to the specific object.
        """
        return (obj.borrower.agent.user == request.user) or request.user.is_staff


class DisbursePermissions(permissions.BasePermission):
    """
    Custom permissions that applies to DisbursementViewSet Disburse endpoint.
    .has_permission is called first, and .has_object_permission is only
    called if the first one returns True
    """
    def has_permission(self, request, view):
        """
        Check if the user is allowed to access the DisbursementViewSet Disburse endpoint
        """
        return request.user.has_perm('loans.zw_disburse_loan')

    def has_object_permission(self, request, view, obj):
        """
        This runs after `has_permission` if it returned True.
        Checks if the user has access to the specific object.
        """
        return (obj.borrower.agent.user == request.user) or request.user.is_staff


class SuperUserOnlyView(permissions.IsAuthenticated):
    """
    permission for SuperUser only views. Staff do not need those views and also cannot access those.
    """
    def has_permission(self, request, view):
        """
        Check if the user is SuperUser
        """
        return hasattr(request.user, 'agent')
