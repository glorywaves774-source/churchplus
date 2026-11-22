from datetime import date

from flask_wtf import FlaskForm
from flask_wtf.file import FileAllowed, FileField, FileRequired
from wtforms import BooleanField, DateField, EmailField, SelectField, StringField, TextAreaField
from wtforms.validators import DataRequired, Email, Length, Optional, Regexp, ValidationError

from app.models import FamilyRole, Gender, MaritalStatus, MemberStatus

TITLES = ["", "Mr", "Mrs", "Miss", "Ms", "Dr", "Prof", "Rev", "Rev. Dr", "Pastor", "Apostle", "Prophet",
          "Bishop", "Elder", "Deacon", "Deaconess", "Evangelist", "Hon", "Nana", "Togbe", "Mama"]


def _choices(enum_cls, blank="Not specified"):
    return [("", blank)] + [(e.value, e.value.replace("_", " ").capitalize()) for e in enum_cls]


def not_future(form, field):
    if field.data and field.data > date.today():
        raise ValidationError("This date is in the future.")


class MemberForm(FlaskForm):
    title = SelectField("Title", choices=[(t, t or "None") for t in TITLES], validators=[Optional()])
    first_name = StringField("First name", validators=[DataRequired(), Length(max=100)])
    middle_name = StringField("Middle name", validators=[Optional(), Length(max=100)])
    last_name = StringField("Surname", validators=[DataRequired(), Length(max=100)])
    gender = SelectField("Gender", choices=_choices(Gender), validators=[Optional()])
    date_of_birth = DateField("Date of birth", validators=[Optional(), not_future])
    marital_status = SelectField("Marital status", choices=_choices(MaritalStatus), validators=[Optional()])
    occupation = StringField("Occupation", validators=[Optional(), Length(max=120)])
    employer = StringField("Employer / business", validators=[Optional(), Length(max=160)])
    nationality = StringField("Nationality", validators=[Optional(), Length(max=80)])
    hometown = StringField("Hometown", validators=[Optional(), Length(max=120)])

    phone = StringField("Phone", validators=[Optional(), Length(max=40)])
    alt_phone = StringField("Other phone", validators=[Optional(), Length(max=40)])
    email = EmailField("Email", validators=[Optional(), Email(check_deliverability=False), Length(max=255)])
    address = StringField("House address", validators=[Optional(), Length(max=255)])
    city = StringField("City or town", validators=[Optional(), Length(max=120)])
    region = StringField("Region", validators=[Optional(), Length(max=120)])
    digital_address = StringField("Ghana Post GPS address", validators=[
        Optional(), Regexp(r"^[A-Za-z]{2}-?\d{3,4}-?\d{3,5}$", message="Use the format GA-123-4567.")])

    emergency_name = StringField("Emergency contact name", validators=[Optional(), Length(max=160)])
    emergency_phone = StringField("Emergency contact phone", validators=[Optional(), Length(max=40)])
    emergency_relation = StringField("Relationship", validators=[Optional(), Length(max=60)])

    branch = SelectField("Branch", validators=[Optional()])
    date_joined = DateField("Date joined", validators=[Optional(), not_future])
    previous_church = StringField("Previous church (if transferred in)", validators=[Optional(), Length(max=200)])
    is_baptised = BooleanField("Baptised")
    baptism_date = DateField("Baptism date", validators=[Optional(), not_future])
    baptism_place = StringField("Place of baptism", validators=[Optional(), Length(max=160)])
    baptised_by = StringField("Baptised by", validators=[Optional(), Length(max=160)])
    allow_birthday_messages = BooleanField("Send birthday messages", default=True)
    notes = TextAreaField("Notes", validators=[Optional(), Length(max=4000)])


class PhotoForm(FlaskForm):
    photo = FileField("Passport photograph", validators=[FileRequired("Choose a photo."),
                                                        FileAllowed(["jpg", "jpeg", "png", "webp"], "JPG, PNG or WEBP only.")])


DOC_CATEGORIES = [("baptism", "Baptism certificate"), ("marriage", "Marriage certificate"),
                  ("transfer", "Transfer letter"), ("id", "Identification"), ("membership", "Membership form"),
                  ("other", "Other")]


class DocumentForm(FlaskForm):
    title = StringField("Document title", validators=[DataRequired(), Length(max=160)])
    category = SelectField("Type", choices=DOC_CATEGORIES)
    file = FileField("File", validators=[FileRequired("Choose a file."),
                                         FileAllowed(["pdf", "jpg", "jpeg", "png", "docx"], "PDF, JPG, PNG or DOCX only.")])


class StatusForm(FlaskForm):
    status = SelectField("New status", choices=[(s.value, s.value.capitalize()) for s in MemberStatus
                                                if s != MemberStatus.TRANSFERRED])
    effective_date = DateField("Effective date", validators=[DataRequired(), not_future], default=date.today)
    reason = StringField("Reason", validators=[Optional(), Length(max=255)])


class TransferForm(FlaskForm):
    kind = SelectField("Transfer type", choices=[("branch", "To another branch of this church"),
                                                 ("out", "To another church")])
    branch = SelectField("New branch", validators=[Optional()])
    other_church = StringField("Receiving church", validators=[Optional(), Length(max=200)])
    effective_date = DateField("Effective date", validators=[DataRequired(), not_future], default=date.today)
    reason = StringField("Reason", validators=[Optional(), Length(max=255)])


class FamilyForm(FlaskForm):
    family = SelectField("Family", validators=[Optional()])
    new_family_name = StringField("Or create a new family", validators=[Optional(), Length(max=160)])
    role = SelectField("Role in family", choices=[(r.value, r.value.capitalize()) for r in FamilyRole])


class ImportForm(FlaskForm):
    file = FileField("Spreadsheet", validators=[FileRequired("Choose a file."),
                                                FileAllowed(["csv", "xlsx"], "Upload a .csv or .xlsx file.")])
    branch = SelectField("Branch for rows without a branch code", validators=[Optional()])
