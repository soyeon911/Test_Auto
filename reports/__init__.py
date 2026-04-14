# reports package
# (lazy imports only — do not eagerly load excel_reporter here,
#  so that openpyxl absence produces a clear error message)
from reports.excel_reporter import ExcelReportBuilder