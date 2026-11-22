import re

from flask_wtf import FlaskForm
from flask_wtf.file import FileAllowed, FileField
from wtforms import BooleanField, EmailField, SelectField, StringField, TextAreaField
from wtforms.validators import DataRequired, Email, Length, Optional, Regexp, ValidationError

from app.auth.forms import COUNTRIES

CURRENCIES = [("GHS", "GHS – Ghana cedi"), ("USD", "USD – US dollar"), ("GBP", "GBP – Pound sterling"),
              ("EUR", "EUR – Euro"), ("NGN", "NGN – Naira"), ("KES", "KES – Kenyan shilling"),
              ("ZAR", "ZAR – Rand"), ("XOF", "XOF – CFA franc"), ("CAD", "CAD – Canadian dollar")]
TIMEZONES = ["Africa/Accra", "Africa/Lagos", "Africa/Abidjan", "Africa/Lome", "Africa/Nairobi",
             "Africa/Johannesburg", "Africa/Kampala", "Africa/Dar_es_Salaam", "Africa/Kigali", "Africa/Lusaka",
             "Africa/Harare", "Africa/Douala", "Africa/Monrovia", "Africa/Freetown", "Africa/Dakar",
             "Europe/London", "Europe/Dublin", "Europe/Berlin", "Europe/Amsterdam", "Europe/Paris",
             "Europe/Rome", "America/New_York", "America/Chicago", "America/Toronto", "America/Los_Angeles",
             "Asia/Dubai", "Australia/Sydney", "UTC"]
HEX = Regexp(r"^#[0-9a-fA-F]{6}$", message="Use a colour like #5b3fd1.")


class ChurchProfileForm(FlaskForm):
    name = StringField("Church name", validators=[DataRequired(), Length(min=3, max=200)])
    legal_name = StringField("Registered (legal) name", validators=[Optional(), Length(max=255)])
    tagline = StringField("Motto or tagline", validators=[Optional(), Length(max=255)])
    email = EmailField("Church email", validators=[Optional(), Email(check_deliverability=False), Length(max=255)])
    phone = StringField("Church phone", validators=[Optional(), Length(max=40)])
    website = StringField("Website", validators=[Optional(), Length(max=255)])
    address_line1 = StringField("Address", validators=[Optional(), Length(max=255)])
    address_line2 = StringField("Address line 2", validators=[Optional(), Length(max=255)])
    city = StringField("City or town", validators=[Optional(), Length(max=120)])
    region = StringField("Region / state", validators=[Optional(), Length(max=120)])
    country = SelectField("Country", choices=COUNTRIES)
    digital_address = StringField("Ghana Post GPS address", validators=[
        Optional(), Regexp(r"^[A-Za-z]{2}-?\d{3,4}-?\d{3,5}$", message="Use the format GA-123-4567.")])
    currency = SelectField("Currency", choices=CURRENCIES)
    timezone = SelectField("Time zone", choices=[(t, t.replace("_", " ")) for t in TIMEZONES])
    primary_color = StringField("Brand colour", validators=[Optional(), HEX])
    accent_color = StringField("Accent colour", validators=[Optional(), HEX])

    def validate_website(self, field):
        if field.data and not re.match(r"^https?://", field.data):
            field.data = "https://" + field.data.strip()


class LogoForm(FlaskForm):
    logo = FileField("Church logo", validators=[DataRequired("Choose an image."),
                                               FileAllowed(["png", "jpg", "jpeg", "webp"], "PNG, JPG or WEBP only.")])


class BranchForm(FlaskForm):
    name = StringField("Branch name", validators=[DataRequired(), Length(min=2, max=200)])
    code = StringField("Short code", description="Used in membership numbers and reports, e.g. TEMA.",
                       validators=[DataRequired(), Length(min=2, max=20),
                                   Regexp(r"^[A-Za-z0-9-]+$", message="Letters, numbers and hyphens only.")])
    email = EmailField("Email", validators=[Optional(), Email(check_deliverability=False), Length(max=255)])
    phone = StringField("Phone", validators=[Optional(), Length(max=40)])
    address = StringField("Address", validators=[Optional(), Length(max=255)])
    city = StringField("City or town", validators=[Optional(), Length(max=120)])
    region = StringField("Region / state", validators=[Optional(), Length(max=120)])
    country = SelectField("Country", choices=COUNTRIES)


class MemberChurchForm(FlaskForm):
    name = StringField("Church name", validators=[DataRequired(), Length(min=3, max=200)])
    city = StringField("City or town", validators=[Optional(), Length(max=120)])
    admin_first = StringField("Administrator's first name", validators=[DataRequired(), Length(max=100)])
    admin_last = StringField("Administrator's last name", validators=[DataRequired(), Length(max=100)])
    admin_email = EmailField("Administrator's email", validators=[DataRequired(), Email(check_deliverability=False)])
