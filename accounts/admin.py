from django.contrib import admin
from .models import User, Customer, StaffReportAccess

admin.site.register(User)
admin.site.register(Customer)
admin.site.register(StaffReportAccess)
