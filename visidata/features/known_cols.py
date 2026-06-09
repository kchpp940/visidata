'''#1488
Usage (in .visidatarc):


    DirSheet.knownCols.directory.width = 0
    Sheet.knownCols.date.type = date
'''

__description__ = "#1488"

from visidata import Sheet, DefaultAttrDict


Sheet.knownCols = DefaultAttrDict()


@Sheet.before
def afterLoad(sheet):
    for colname, attrs in sheet.knownCols.items():
        col = sheet.colsByName.get(colname)
        if col:
            for k, v in attrs.items():
                setattr(col, k, v)