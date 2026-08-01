from django.db import transaction
from rest_framework import generics, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import Business
from apps.accounts.pagination import paginated_response
from apps.sales.services import recalculate_balances

from .models import Customer
from .serializers import CustomerNoteSerializer, CustomerSerializer


def _business_or_error(request):
    try:
        return request.user.business, None
    except Business.DoesNotExist:
        return None, Response(
            {"success": False, "error": {"code": "NO_BUSINESS", "message": "No business created yet."}},
            status=status.HTTP_404_NOT_FOUND,
        )


class CustomerListCreateView(generics.ListCreateAPIView):
    serializer_class = CustomerSerializer

    def get_queryset(self):
        return Customer.objects.filter(business=self.request.user.business).order_by("name")

    def list(self, request, *args, **kwargs):
        business, error = _business_or_error(request)
        if error:
            return error
        # Paginated: a shop with a few thousand customers would otherwise
        # serialize the entire book on every list call.
        return Response(paginated_response(CustomerSerializer, self.get_queryset(), request))

    def create(self, request, *args, **kwargs):
        business, error = _business_or_error(request)
        if error:
            return error
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save(business=business)
        return Response({"success": True, "data": serializer.data}, status=status.HTTP_201_CREATED)


class CustomerDetailView(generics.RetrieveUpdateAPIView):
    serializer_class = CustomerSerializer

    def get_queryset(self):
        return Customer.objects.filter(business=self.request.user.business)

    def retrieve(self, request, *args, **kwargs):
        business, error = _business_or_error(request)
        if error:
            return error
        instance = self.get_object()
        return Response({"success": True, "data": self.get_serializer(instance).data})

    def update(self, request, *args, **kwargs):
        business, error = _business_or_error(request)
        if error:
            return error
        instance = self.get_object()
        previous_opening_balance = instance.opening_balance
        serializer = self.get_serializer(instance, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)

        # Every entry's balance_after is derived from opening_balance, so
        # changing it invalidates the whole ledger for this customer. Editing it
        # previously just wrote the new figure and left current_balance and
        # every historical balance_after showing the old arithmetic.
        with transaction.atomic():
            customer = serializer.save()
            if customer.opening_balance != previous_opening_balance:
                recalculate_balances(customer)

        return Response({"success": True, "data": self.get_serializer(customer).data})


class CustomerNoteView(APIView):
    def get_customer(self, request, pk):
        return Customer.objects.get(business=request.user.business, pk=pk)

    def get(self, request, pk):
        business, error = _business_or_error(request)
        if error:
            return error
        try:
            customer = self.get_customer(request, pk)
        except Customer.DoesNotExist:
            return Response(
                {"success": False, "error": {"code": "NOT_FOUND", "message": "Customer not found."}},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response({"success": True, "data": {"note": customer.note or ""}})

    def patch(self, request, pk):
        business, error = _business_or_error(request)
        if error:
            return error
        try:
            customer = self.get_customer(request, pk)
        except Customer.DoesNotExist:
            return Response(
                {"success": False, "error": {"code": "NOT_FOUND", "message": "Customer not found."}},
                status=status.HTTP_404_NOT_FOUND,
            )
        serializer = CustomerNoteSerializer(customer, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response({"success": True, "data": {"note": customer.note or ""}})
