from flask_wtf import FlaskForm
from wtforms import BooleanField, EmailField, PasswordField, RadioField, SelectField, StringField
from wtforms.validators import DataRequired, Email, EqualTo, Length, Optional, ValidationError

COUNTRIES = [
    ("GH", "Ghana"), ("NG", "Nigeria"), ("KE", "Kenya"), ("ZA", "South Africa"), ("CI", "Côte d'Ivoire"),
    ("TG", "Togo"), ("BJ", "Benin"), ("SN", "Senegal"), ("LR", "Liberia"), ("SL", "Sierra Leone"),
    ("UG", "Uganda"), ("TZ", "Tanzania"), ("RW", "Rwanda"), ("ZM", "Zambia"), ("ZW", "Zimbabwe"),
    ("CM", "Cameroon"), ("GB", "United Kingdom"), ("IE", "Ireland"), ("US", "United States"),
    ("CA", "Canada"), ("DE", "Germany"), ("NL", "Netherlands"), ("IT", "Italy"), ("FR", "France"),
    ("AU", "Australia"), ("AE", "United Arab Emirates"),
]
CURRENCY_BY_COUNTRY = {
    "GH": "GHS", "NG": "NGN", "KE": "KES", "ZA": "ZAR", "CI": "XOF", "TG": "XOF", "BJ": "XOF", "SN": "XOF",
    "GB": "GBP", "US": "USD", "CA": "CAD", "IE": "EUR", "DE": "EUR", "NL": "EUR", "IT": "EUR", "FR": "EUR",
}
TIMEZONE_BY_COUNTRY = {
    "GH": "Africa/Accra", "NG": "Africa/Lagos", "KE": "Africa/Nairobi", "ZA": "Africa/Johannesburg",
    "CI": "Africa/Abidjan", "TG": "Africa/Lome", "BJ": "Africa/Porto-Novo", "SN": "Africa/Dakar",
    "GB": "Europe/London", "IE": "Europe/Dublin", "US": "America/New_York", "CA": "America/Toronto",
    "DE": "Europe/Berlin", "NL": "Europe/Amsterdam", "IT": "Europe/Rome", "FR": "Europe/Paris",
    "UG": "Africa/Kampala", "TZ": "Africa/Dar_es_Salaam", "RW": "Africa/Kigali", "ZM": "Africa/Lusaka",
    "ZW": "Africa/Harare", "CM": "Africa/Douala", "LR": "Africa/Monrovia", "SL": "Africa/Freetown",
    "AU": "Australia/Sydney", "AE": "Asia/Dubai",
}


def strong_password(form, field):
    value = field.data or ""
    if len(value) < 10:
        raise ValidationError("Use at least 10 characters.")
    kinds = sum([any(c.islower() for c in value), any(c.isupper() for c in value),
                 any(c.isdigit() for c in value), any(not c.isalnum() for c in value)])
    if kinds < 3:
        raise ValidationError("Mix at least three of: lowercase, uppercase, numbers, symbols.")


class LoginForm(FlaskForm):
    email = EmailField("Email address", validators=[DataRequired(), Email(check_deliverability=False)])
    password = PasswordField("Password", validators=[DataRequired()])
    remember = BooleanField("Keep me signed in on this device")


class RegisterChurchForm(FlaskForm):
    church_name = StringField("Church name", validators=[DataRequired(), Length(min=3, max=200)])
    kind = RadioField("Type of organisation", default="independent", choices=[
        ("independent", "A church (with or without branches)"),
        ("denomination_hq", "Denomination headquarters overseeing several churches"),
    ])
    country = SelectField("Country", choices=COUNTRIES, default="GH")
    city = StringField("City or town", validators=[DataRequired(), Length(max=120)])
    church_phone = StringField("Church phone", validators=[Optional(), Length(max=40)])
    plan = SelectField("Plan", default="starter")
    first_name = StringField("Your first name", validators=[DataRequired(), Length(max=100)])
    last_name = StringField("Your last name", validators=[DataRequired(), Length(max=100)])
    email = EmailField("Your email address", validators=[DataRequired(), Email(check_deliverability=False), Length(max=255)])
    password = PasswordField("Create a password", validators=[DataRequired(), strong_password])
    confirm = PasswordField("Confirm password", validators=[DataRequired(), EqualTo("password", "Passwords do not match.")])
    accept = BooleanField("I agree to the terms of service and privacy policy, and confirm I am authorised to register this church.",
                          validators=[DataRequired("Please confirm to continue.")])


class SetPasswordForm(FlaskForm):
    password = PasswordField("New password", validators=[DataRequired(), strong_password])
    confirm = PasswordField("Confirm password", validators=[DataRequired(), EqualTo("password", "Passwords do not match.")])


class ForgotPasswordForm(FlaskForm):
    email = EmailField("Email address", validators=[DataRequired(), Email(check_deliverability=False)])


class TwoFactorForm(FlaskForm):
    code = StringField("Authentication code", validators=[DataRequired(), Length(min=6, max=12)],
                       render_kw={"inputmode": "numeric", "autocomplete": "one-time-code"})


class ProfileForm(FlaskForm):
    first_name = StringField("First name", validators=[DataRequired(), Length(max=100)])
    last_name = StringField("Last name", validators=[DataRequired(), Length(max=100)])
    phone = StringField("Phone", validators=[Optional(), Length(max=40)])


class ChangePasswordForm(FlaskForm):
    current = PasswordField("Current password", validators=[DataRequired()])
    password = PasswordField("New password", validators=[DataRequired(), strong_password])
    confirm = PasswordField("Confirm new password", validators=[DataRequired(), EqualTo("password", "Passwords do not match.")])


class ConfirmPasswordForm(FlaskForm):
    password = PasswordField("Your password", validators=[DataRequired()])


class EnableTwoFactorForm(FlaskForm):
    code = StringField("Code from your authenticator app", validators=[DataRequired(), Length(min=6, max=8)],
                       render_kw={"inputmode": "numeric", "autocomplete": "one-time-code"})


class InviteUserForm(FlaskForm):
    first_name = StringField("First name", validators=[DataRequired(), Length(max=100)])
    last_name = StringField("Last name", validators=[DataRequired(), Length(max=100)])
    email = EmailField("Email address", validators=[DataRequired(), Email(check_deliverability=False), Length(max=255)])
    branch = SelectField("Branch", coerce=str, validators=[Optional()])


class EditAccountForm(FlaskForm):
    branch = SelectField("Branch", coerce=str, validators=[Optional()])


class RoleForm(FlaskForm):
    name = StringField("Role name", validators=[DataRequired(), Length(min=2, max=80)])
    description = StringField("Description", validators=[Optional(), Length(max=255)])
    is_branch_scoped = BooleanField("Limit to the person's own branch")
