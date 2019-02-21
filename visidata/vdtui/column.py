import collections
import itertools
import threading
import re

from visidata.vdtui import option, options, fail, anytype, stacktrace, status
from visidata.vdtui import getType, typemap, isNumeric, isNullFunc
from visidata.vdtui import asyncthread, exceptionCaught
from visidata.vdtui import wrapply, TypedWrapper, TypedExceptionWrapper, DisplayWrapper
from visidata.vdtui import Extensible, LazyMap, AttrDict

option('col_cache_size', 0, 'max number of cache entries in each cached column')

__all__ = [
    'clean_to_id',
    'Column',
    'setitem',
    'getattrdeep',
    'setattrdeep',
    'getitemdef',
    'ColumnAttr',
    'ColumnItem',
    'SubrowColumn',
    'ColumnEnum',
    'LazyMapRow',
    'ColumnExpr',
]


def clean_to_id(s):  # [Nas Banov] https://stackoverflow.com/a/3305731
    return re.sub(r'\W|^(?=\d)', '_', str(s))


class Column(Extensible):
    def __init__(self, name='', *, type=anytype, cache=False, **kwargs):
        self.sheet = None     # owning Sheet, set in Sheet.addColumn
        self.name = name      # display visible name
        self.fmtstr = ''      # by default, use str()
        self.type = type      # anytype/str/int/float/date/func
        self.getter = lambda col, row: row
        self.setter = lambda col, row, value: fail(col.name+' column cannot be changed')
        self.width = None     # == 0 if hidden, None if auto-compute next time
        self.keycol = False   # is a key column
        self.expr = None      # Column-type-dependent parameter

        self.setCache(cache)
        for k, v in kwargs.items():
            setattr(self, k, v)  # instead of __dict__.update(kwargs) to invoke property.setters

    def __copy__(self):
        cls = self.__class__
        ret = cls.__new__(cls)
        ret.__dict__.update(self.__dict__)
        ret.keycol = False   # column copies lose their key status
        if self._cachedValues is not None:
            ret._cachedValues = collections.OrderedDict()  # an unrelated cache for copied columns
        return ret

    def __deepcopy__(self, memo):
        return self.__copy__()  # no separate deepcopy

    def recalc(self, sheet=None):
        'reset column cache, attach to sheet, and reify name'
        if self._cachedValues:
            self._cachedValues.clear()
        if sheet:
            self.sheet = sheet
        self.name = self._name

    @property
    def name(self):
        return self._name or ''

    @name.setter
    def name(self, name):
        if isinstance(name, str):
            name = name.strip()
        if options.force_valid_colnames:
            name = clean_to_id(name)
        self._name = name

    @property
    def fmtstr(self):
        return self._fmtstr or getType(self.type).fmtstr

    @fmtstr.setter
    def fmtstr(self, v):
        self._fmtstr = v

    def format(self, typedval):
        'Return displayable string of `typedval` according to `Column.fmtstr`'
        if typedval is None:
            return None

        if isinstance(typedval, (list, tuple)):
            return '[%s]' % len(typedval)
        if isinstance(typedval, dict):
            return '{%s}' % len(typedval)
        if isinstance(typedval, bytes):
            typedval = typedval.decode(options.encoding, options.encoding_errors)

        return getType(self.type).formatter(self.fmtstr, typedval)

    def hide(self, hide=True):
        if hide:
            self.width = 0
        else:
            self.width = abs(self.width or self.getMaxWidth(self.sheet.visibleRows))

    @property
    def hidden(self):
        'A column is hidden if its width <= 0. (width==None means not-yet-autocomputed).'
        if self.width is None:
            return False
        return self.width <= 0

    def calcValue(self, row):
        return (self.getter)(self, row)

    def getTypedValue(self, row):
        'Returns the properly-typed value for the given row at this column, or a TypedWrapper object.'
        return wrapply(self.type, wrapply(self.getValue, row))

    def setCache(self, cache):
        'cache=False: always call CalcValue; True: maintain cache of options.col_cache_size; "async": maintain an infinite cache and launch threads'
        self.cache = cache
        self._cachedValues = collections.OrderedDict() if self.cache else None

    @asyncthread
    def _putValue_async(self, row):
        self._cachedValues[id(row)] = None
        self._putValue(row)

    def _putValue(self, row):
        ret = wrapply(self.calcValue, row)
        self._cachedValues[id(row)] = ret
        return ret

    def getValue(self, row):
        'Memoize calcValue with key id(row)'
        if self._cachedValues is None:
            return self.calcValue(row)

        k = id(row)
        if k in self._cachedValues:
            return self._cachedValues[k]

        if self.cache == 'async':
            ret = self._putValue_async(row)
        else:
            ret = self._putValue(row)

            cachesize = options.col_cache_size
            if cachesize > 0 and len(self._cachedValues) > cachesize:
                self._cachedValues.popitem(last=False)

        return ret

    def getCell(self, row, width=None):
        'Return DisplayWrapper for displayable cell value.'
        cellval = wrapply(self.getValue, row)
        typedval = wrapply(self.type, cellval)

        if isinstance(typedval, TypedWrapper):
            if isinstance(cellval, TypedExceptionWrapper):  # calc failed
                exc = cellval.exception
                if cellval.forwarded:
                    dispval = str(cellval)  # traceback.format_exception_only(type(exc), exc)[-1].strip()
                else:
                    dispval = options.disp_error_val
                return DisplayWrapper(cellval.val, error=exc.stacktrace,
                                        display=dispval,
                                        note=options.note_getter_exc,
                                        notecolor='color_error')
            elif typedval.val is None:  # early out for strict None
                return DisplayWrapper(None, display='',  # force empty display for None
                                            note=options.disp_note_none,
                                            notecolor='color_note_type')
            elif isinstance(typedval, TypedExceptionWrapper):  # calc succeeded, type failed
                return DisplayWrapper(typedval.val, display=str(cellval),
                                            error=typedval.exception.stacktrace,
                                            note=options.note_type_exc,
                                            notecolor='color_warning')
            else:
                return DisplayWrapper(typedval.val, display=str(typedval.val),
                                            note=options.note_type_exc,
                                            notecolor='color_warning')

        elif isinstance(typedval, threading.Thread):
            return DisplayWrapper(None,
                                display=options.disp_pending,
                                note=options.note_pending,
                                notecolor='color_note_pending')

        dw = DisplayWrapper(cellval)

        try:
            dw.display = self.format(typedval) or ''

            if width and isNumeric(self):
                dw.display = dw.display.rjust(width-1)

            # annotate cells with raw value type in anytype columns, except for strings
            if self.type is anytype and type(cellval) is not str:
                typedesc = typemap.get(type(cellval), None)
                dw.note = typedesc.icon if typedesc else options.note_unknown_type
                dw.notecolor = 'color_note_type'

        except Exception as e:  # formatting failure
            e.stacktrace = stacktrace()
            dw.error = e
            try:
                dw.display = str(cellval)
            except Exception as e:
                dw.display = str(e)
            dw.note = options.note_format_exc
            dw.notecolor = 'color_warning'

        return dw

    def getDisplayValue(self, row):
        return self.getCell(row).display

    def setValue(self, row, value):
        'Set our column value on row.  defaults to .setter; override in Column subclass. no type checking'
        return self.setter(self, row, value)

    def setValueSafe(self, row, value):
        'setValue and ignore exceptions'
        try:
            return self.setValue(row, value)
        except Exception as e:
            exceptionCaught(e)

    def setValues(self, rows, *values):
        'Set our column value for given list of rows to `value`.'
        for r, v in zip(rows, itertools.cycle(values)):
            self.setValueSafe(r, v)
        self.recalc()
        return status('set %d cells to %d values' % (len(rows), len(values)))

    def setValuesTyped(self, rows, *values):
        'Set values on this column for rows, coerced to the column type.  will stop on first exception in type().'
        for r, v in zip(rows, itertools.cycle(self.type(val) for val in values)):
            self.setValueSafe(r, v)
        self.recalc()
        return status('set %d cells to %d values' % (len(rows), len(values)))

    def getMaxWidth(self, rows):
        'Return the maximum length of any cell in column or its header.'
        w = 0
        if len(rows) > 0:
            w = max(max(len(self.getDisplayValue(r)) for r in rows), len(self.name))+2
        return max(w, len(self.name))

    def toggleWidth(self, width):
        'Change column width to either given `width` or default value.'
        if self.width != width:
            self.width = width
        else:
            self.width = int(options.default_width)


# ---- Column makers

def setitem(r, i, v):  # function needed for use in lambda
    r[i] = v
    return True

def getattrdeep(obj, attr, *default):
    'Return dotted attr (like "a.b.c") from obj, or default if any of the components are missing.'
    attrs = attr.split('.')
    if default:
        getattr_default = lambda o,a,d=default[0]: getattr(o, a, d)
    else:
        getattr_default = lambda o,a: getattr(o, a)

    for a in attrs[:-1]:
        obj = getattr_default(obj, a)

    return getattr_default(obj, attrs[-1])

def setattrdeep(obj, attr, val):
    'Set dotted attr (like "a.b.c") on obj to val.'
    attrs = attr.split('.')

    for a in attrs[:-1]:
        obj = getattr(obj, a)
    setattr(obj, attrs[-1], val)


def ColumnAttr(name='', attr=None, **kwargs):
    'Column using getattr/setattr of given attr.'
    return Column(name,
                  expr=attr if attr is not None else name,
                  getter=lambda col,row: getattrdeep(row, col.expr),
                  setter=lambda col,row,val: setattrdeep(row, col.expr, val),
                  **kwargs)

def getitemdef(o, k, default=None):
    try:
        return default if o is None else o[k]
    except Exception:
        return default

def ColumnItem(name='', key=None, **kwargs):
    'Column using getitem/setitem of given key.'
    return Column(name,
            expr=key if key is not None else name,
            getter=lambda col,row: getitemdef(row, col.expr),
            setter=lambda col,row,val: setitem(row, col.expr, val),
            **kwargs)


class SubrowColumn(Column):
    def __init__(self, name, origcol, subrowidx, **kwargs):
        super().__init__(name, type=origcol.type, width=origcol.width, **kwargs)
        self.origcol = origcol
        self.expr = subrowidx

    def getValue(self, row):
        subrow = row[self.expr]
        if subrow is not None:
            return self.origcol.getValue(subrow)

    def setValue(self, row, value):
        subrow = row[self.expr]
        if subrow is None:
            fail('no source row')
        self.origcol.setValue(subrow, value)

    def recalc(self, sheet=None):
        Column.recalc(self, sheet)
        self.origcol.recalc()  # reset cache but don't change sheet


class ColumnEnum(Column):
    'types and aggregators. row.<name> should be kept to the values in the mapping m, and can be set by the a string key into the mapping.'
    def __init__(self, name, m, default=None):
        super().__init__(name)
        self.mapping = m
        self.default = default

    def getValue(self, row):
        v = getattr(row, self.name, None)
        return v.__name__ if v else None

    def setValue(self, row, value):
        if isinstance(value, str):  # first try to get the actual value from the mapping
            value = self.mapping.get(value, value)
        setattr(row, self.name, value or self.default)

class LazyMapRow(LazyMap):
    'Calculate column values as needed.'
    def __init__(self, sheet, row):
        super().__init__(sheet, AttrDict(row=row, sheet=sheet))
        self.row = row
        self.sheet = sheet
        self._keys = [c.name for c in self.sheet.columns]

    def keys(self):
        return self._keys

    def __getitem__(self, colid):
        try:
            i = self._keys.index(colid)
            return self.sheet.columns[i].getTypedValue(self.row)
        except ValueError:
            return super().__getitem__(colid)


class ColumnExpr(Column):
    def __init__(self, name, cache=True, expr=None, **kwargs):
        super().__init__(name, **kwargs)
        self.expr = expr or name

    def calcValue(self, row):
        return self.sheet.evalexpr(self.compiledExpr, row)

    @property
    def expr(self):
        return self._expr

    @expr.setter
    def expr(self, expr):
        try:
            self.compiledExpr = compile(expr, '<expr>', 'eval') if expr else None
            self._expr = expr
        except SyntaxError as e:
            self._expr = None
            pass
