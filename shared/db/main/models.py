# packages เรียงชื่อจาก A-Z
# import ต้องขึ้นก่อน from หากอยู่ในหมวดเดียวกัน
# objects ที่ import มาให้เรียงชื่อจาก A-Z โดยมีลำดับ: abc (fucntions, variables) , Abc (classes) , ABC (constants)

# packages ที่มากับ Python อยู่แล้ว
from decimal import Decimal

# packages ที่ install เข้ามา
from sqlalchemy import text, CheckConstraint, ForeignKey, Numeric, String, Text
from sqlalchemy.orm import mapped_column, relationship, Mapped

# packages ที่เขียนเอง และใช้ร่วมกันใน Project
from shared.db.main.base import Base
from shared.db.mixins import TimestampMixin

# ทุก ForeignKey ใช้ ondelete="RESTRICT" = ลบไม่ได้ถ้ายังมีแถวอื่นผูกอยู่
# ฝั่ง relationship ใส่ passive_deletes="all" ให้ SQLAlchemy ไม่ไปแก้แถวลูกเอง ปล่อยให้ DB เป็นคนห้าม

class Department(TimestampMixin, Base):
    __tablename__ = "departments"

    department_index: Mapped[int] = mapped_column(primary_key=True)
    department_name: Mapped[str] = mapped_column(String(), unique=True)

    asset_holdings: Mapped[list["AssetHolding"]] = relationship(
        back_populates="department", passive_deletes="all"
    )
    asset_requisition_orders: Mapped[list["AssetRequisitionOrder"]] = relationship(
        back_populates="department", passive_deletes="all"
    )
    employees: Mapped[list["Employee"]] = relationship(
        back_populates="department", passive_deletes="all"
    )

class Employee(TimestampMixin, Base):
    __tablename__ = "employees"

    employee_index: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[str] = mapped_column(String(), unique=True)
    department_index: Mapped[int | None] = mapped_column(
        ForeignKey("departments.department_index", ondelete="RESTRICT"),
        index=True
    )
    user_password_hash: Mapped[str] = mapped_column(
        String(),
        server_default=text("'!' || gen_random_uuid()::text")
    )

    admin: Mapped["Admin | None"] = relationship(
        back_populates="employee", passive_deletes="all"
    )
    asset_holdings: Mapped[list["AssetHolding"]] = relationship(
        back_populates="employee", passive_deletes="all"
    )
    asset_requisition_order_comments: Mapped[list["AssetRequisitionOrderComment"]] = relationship(
        back_populates="employee", passive_deletes="all"
    )
    asset_requisition_orders: Mapped[list["AssetRequisitionOrder"]] = relationship(
        back_populates="employee", passive_deletes="all"
    )
    department: Mapped["Department | None"] = relationship(back_populates="employees")
    line_users: Mapped[list["LineUser"]] = relationship(
        back_populates="employee", passive_deletes="all"
    )
    meter_report_order_comments: Mapped[list["MeterReportOrderComment"]] = relationship(
        back_populates="employee", passive_deletes="all"
    )
    meter_report_orders: Mapped[list["MeterReportOrder"]] = relationship(
        back_populates="employee", passive_deletes="all"
    )

class LineUser(TimestampMixin, Base):
    __tablename__ = "line_users"

    line_user_index: Mapped[int] = mapped_column(primary_key=True)
    line_user_id: Mapped[str] = mapped_column(String(), unique=True)
    employee_index: Mapped[int] = mapped_column(
        ForeignKey("employees.employee_index", ondelete="RESTRICT"),
        index=True
    )

    employee: Mapped["Employee"] = relationship(back_populates="line_users")

class Admin(TimestampMixin, Base):
    __tablename__ = "admins"

    admin_index: Mapped[int] = mapped_column(primary_key=True)
    employee_index: Mapped[int] = mapped_column(
        ForeignKey("employees.employee_index", ondelete="RESTRICT"),
        unique=True
    )

    employee: Mapped["Employee"] = relationship(back_populates="admin")
    asset_requisition_orders: Mapped[list["AssetRequisitionOrder"]] = relationship(
        back_populates="admin", passive_deletes="all"
    )
    meter_report_orders: Mapped[list["MeterReportOrder"]] = relationship(
        back_populates="admin", passive_deletes="all"
    )

class MeterReportOrderStatus(TimestampMixin, Base):
    __tablename__ = "meter_report_order_statuses"

    meter_report_order_status_index: Mapped[int] = mapped_column(primary_key=True)
    meter_report_order_status_name: Mapped[str] = mapped_column(String(), unique=True)
    meter_report_order_status_is_default: Mapped[bool] = mapped_column(
        default=False,
        server_default=text("false")
    )

    meter_report_orders: Mapped[list["MeterReportOrder"]] = relationship(
        back_populates="meter_report_order_status", passive_deletes="all"
    )

class MeterReportOrder(TimestampMixin, Base):
    __tablename__ = "meter_report_orders"

    meter_report_order_index: Mapped[int] = mapped_column(primary_key=True)

    employee_index: Mapped[int] = mapped_column(
        ForeignKey("employees.employee_index", ondelete="RESTRICT"),
        index=True
    )
    meter_id: Mapped[str] = mapped_column(String(), index=True)
    project_name: Mapped[str | None] = mapped_column(String())
    issue_detail: Mapped[str | None] = mapped_column(Text)

    meter_report_order_status_index: Mapped[int] = mapped_column(
        ForeignKey("meter_report_order_statuses.meter_report_order_status_index", ondelete="RESTRICT"),
        index=True
    )

    admin_index: Mapped[int | None] = mapped_column(
        ForeignKey("admins.admin_index", ondelete="RESTRICT"),
        index=True
    )

    admin: Mapped["Admin | None"] = relationship(back_populates="meter_report_orders")
    employee: Mapped["Employee"] = relationship(back_populates="meter_report_orders")
    meter_report_order_comments: Mapped[list["MeterReportOrderComment"]] = relationship(
        back_populates="meter_report_order", passive_deletes="all"
    )
    meter_report_order_status: Mapped["MeterReportOrderStatus"] = relationship(back_populates="meter_report_orders")

class MeterReportOrderComment(TimestampMixin, Base):
    __tablename__ = "meter_report_order_comments"

    meter_report_order_comment_index: Mapped[int] = mapped_column(primary_key=True)

    meter_report_order_index: Mapped[int] = mapped_column(
        ForeignKey("meter_report_orders.meter_report_order_index", ondelete="RESTRICT"),
        index=True
    )
    employee_index: Mapped[int] = mapped_column(
        ForeignKey("employees.employee_index", ondelete="RESTRICT"),
        index=True
    )
    comment_detail: Mapped[str] = mapped_column(Text)

    employee: Mapped["Employee"] = relationship(back_populates="meter_report_order_comments")
    meter_report_order: Mapped["MeterReportOrder"] = relationship(back_populates="meter_report_order_comments")

class AssetUnit(TimestampMixin, Base):
    __tablename__ = "asset_units"

    asset_unit_index: Mapped[int] = mapped_column(primary_key=True)
    asset_unit_name: Mapped[str] = mapped_column(String(), unique=True)

    assets: Mapped[list["Asset"]] = relationship(
        back_populates="asset_unit", passive_deletes="all"
    )

class Asset(TimestampMixin, Base):
    __tablename__ = "assets"

    asset_index: Mapped[int] = mapped_column(primary_key=True)
    asset_id: Mapped[str] = mapped_column(String(), unique=True)
    asset_name: Mapped[str] = mapped_column(String())
    asset_unit_index: Mapped[int] = mapped_column(
        ForeignKey("asset_units.asset_unit_index", ondelete="RESTRICT"),
        index=True
    )
    asset_price: Mapped[Decimal] = mapped_column(Numeric(12, 2))

    asset_holdings: Mapped[list["AssetHolding"]] = relationship(
        back_populates="asset", passive_deletes="all"
    )
    asset_requisition_order_details: Mapped[list["AssetRequisitionOrderDetail"]] = relationship(
        back_populates="asset", passive_deletes="all"
    )
    asset_unit: Mapped["AssetUnit"] = relationship(back_populates="assets")

class AssetRequisitionOrderStatus(TimestampMixin, Base):
    __tablename__ = "asset_requisition_order_statuses"

    asset_requisition_order_status_index: Mapped[int] = mapped_column(primary_key=True)
    asset_requisition_order_status_name: Mapped[str] = mapped_column(String(), unique=True)
    asset_requisition_order_status_is_default: Mapped[bool] = mapped_column(
        default=False,
        server_default=text("false")
    )
    asset_requisition_order_status_is_completed: Mapped[bool] = mapped_column(
        default=False,
        server_default=text("false")
    )

    asset_requisition_orders: Mapped[list["AssetRequisitionOrder"]] = relationship(
        back_populates="asset_requisition_order_status", passive_deletes="all"
    )

class AssetRequisitionOrder(TimestampMixin, Base):
    __tablename__ = "asset_requisition_orders"

    asset_requisition_order_index: Mapped[int] = mapped_column(primary_key=True)

    employee_index: Mapped[int] = mapped_column(
        ForeignKey("employees.employee_index", ondelete="RESTRICT"),
        index=True
    )
    department_index: Mapped[int | None] = mapped_column(
        ForeignKey("departments.department_index", ondelete="RESTRICT"),
        index=True
    )

    asset_requisition_order_status_index: Mapped[int] = mapped_column(
        ForeignKey("asset_requisition_order_statuses.asset_requisition_order_status_index", ondelete="RESTRICT"),
        index=True
    )

    admin_index: Mapped[int | None] = mapped_column(
        ForeignKey("admins.admin_index", ondelete="RESTRICT"),
        index=True
    )

    admin: Mapped["Admin | None"] = relationship(back_populates="asset_requisition_orders")
    asset_requisition_order_comments: Mapped[list["AssetRequisitionOrderComment"]] = relationship(
        back_populates="asset_requisition_order", passive_deletes="all"
    )
    asset_requisition_order_details: Mapped[list["AssetRequisitionOrderDetail"]] = relationship(
        back_populates="asset_requisition_order", passive_deletes="all"
    )
    asset_requisition_order_status: Mapped["AssetRequisitionOrderStatus"] = relationship(
        back_populates="asset_requisition_orders"
    )
    department: Mapped["Department | None"] = relationship(back_populates="asset_requisition_orders")
    employee: Mapped["Employee"] = relationship(back_populates="asset_requisition_orders")

class AssetRequisitionOrderDetail(TimestampMixin, Base):
    __tablename__ = "asset_requisition_order_details"

    asset_requisition_order_detail_index: Mapped[int] = mapped_column(primary_key=True)

    asset_requisition_order_index: Mapped[int] = mapped_column(
        ForeignKey("asset_requisition_orders.asset_requisition_order_index", ondelete="RESTRICT"),
        index=True
    )
    asset_index: Mapped[int] = mapped_column(
        ForeignKey("assets.asset_index", ondelete="RESTRICT"),
        index=True
    )
    asset_quantity: Mapped[int] = mapped_column()
    asset_price: Mapped[Decimal] = mapped_column(Numeric(12, 2))

    asset: Mapped["Asset"] = relationship(back_populates="asset_requisition_order_details")
    asset_holdings: Mapped[list["AssetHolding"]] = relationship(
        back_populates="asset_requisition_order_detail", passive_deletes="all"
    )
    asset_requisition_order: Mapped["AssetRequisitionOrder"] = relationship(
        back_populates="asset_requisition_order_details"
    )

class AssetRequisitionOrderComment(TimestampMixin, Base):
    __tablename__ = "asset_requisition_order_comments"

    asset_requisition_order_comment_index: Mapped[int] = mapped_column(primary_key=True)

    asset_requisition_order_index: Mapped[int] = mapped_column(
        ForeignKey("asset_requisition_orders.asset_requisition_order_index", ondelete="RESTRICT"),
        index=True
    )
    employee_index: Mapped[int] = mapped_column(
        ForeignKey("employees.employee_index", ondelete="RESTRICT"),
        index=True
    )
    comment_detail: Mapped[str] = mapped_column(Text)

    asset_requisition_order: Mapped["AssetRequisitionOrder"] = relationship(
        back_populates="asset_requisition_order_comments"
    )
    employee: Mapped["Employee"] = relationship(back_populates="asset_requisition_order_comments")

class AssetHolding(TimestampMixin, Base):
    __tablename__ = "asset_holdings"
    __table_args__ = (
        CheckConstraint(
            "num_nonnulls(employee_index, department_index) = 1",
            name="ck_asset_holdings_one_owner"
        ),
    )

    asset_holding_index: Mapped[int] = mapped_column(primary_key=True)

    asset_index: Mapped[int] = mapped_column(
        ForeignKey("assets.asset_index", ondelete="RESTRICT"),
        index=True
    )
    employee_index: Mapped[int | None] = mapped_column(
        ForeignKey("employees.employee_index", ondelete="RESTRICT"),
        index=True
    )
    department_index: Mapped[int | None] = mapped_column(
        ForeignKey("departments.department_index", ondelete="RESTRICT"),
        index=True
    )
    asset_requisition_order_detail_index: Mapped[int | None] = mapped_column(
        ForeignKey("asset_requisition_order_details.asset_requisition_order_detail_index", ondelete="RESTRICT"),
        index=True
    )

    asset: Mapped["Asset"] = relationship(back_populates="asset_holdings")
    asset_requisition_order_detail: Mapped["AssetRequisitionOrderDetail | None"] = relationship(
        back_populates="asset_holdings"
    )
    department: Mapped["Department | None"] = relationship(back_populates="asset_holdings")
    employee: Mapped["Employee | None"] = relationship(back_populates="asset_holdings")
