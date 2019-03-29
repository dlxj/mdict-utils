
import sqlite3
import struct
import os.path
import locale

from tqdm import tqdm

from .base.writemdict import MDictWriter as MDictWriterBase, \
    _MdxRecordBlock as _MdxRecordBlockBase,  \
    _OffsetTableEntry as _OffsetTableEntryBase


MDX_OBJ = {}


def get_record_null(mdx_file, key, pos, size, encoding):
    global MDX_OBJ
    if mdx_file not in MDX_OBJ:
        if mdx_file.endswith('.db'):
            conn = sqlite3.connect(mdx_file)
            MDX_OBJ[mdx_file] = conn
        else:
            f = open(mdx_file, 'rb')
            MDX_OBJ[mdx_file] = f
    obj = MDX_OBJ[mdx_file]
    if mdx_file.endswith('.db'):
        sql = 'SELECT paraphrase FROM mdx_txt WHERE entry=?'
        c = obj.execute(sql, (key,))
        for row in c.fetchall():    # multi entry
            record_null = (row[0] + '\0').encode(encoding)
            if len(record_null) == size:
                return record_null
    else:
        obj.seek(pos)
        record_null = obj.read(size - 1)
        return record_null + b'\0'
    return b''


class _OffsetTableEntry(_OffsetTableEntryBase):
    def __init__(self, key0, key, key_null, key_len, offset,
                 record_pos, record_null, record_size, encoding, is_mdd):
        super(_OffsetTableEntry, self).__init__(
            key, key_null, key_len, offset, record_null)
        self.key0 = key0
        self.record_pos = record_pos
        self.record_size = record_size
        self.encoding = encoding
        self.is_mdd = is_mdd

    def get_record_null(self):
        if self.is_mdd:
            return open(self.record_null, 'rb').read()
        else:
            return get_record_null(
                self.record_null, self.key0,
                self.record_pos, self.record_size, self.encoding)


class _MdxRecordBlock(_MdxRecordBlockBase):
    def __init__(self, offset_table, compression_type, version):
        self._offset_table = offset_table
        self._compression_type = compression_type
        self._version = version

    def prepare(self):
        super(_MdxRecordBlock, self).__init__(
            self._offset_table, self._compression_type, self._version)

    def clean(self):
        if self._comp_data:
            self._comp_data = None

    @staticmethod
    def _block_entry(t, version):
        return t.get_record_null()

    @staticmethod
    def _len_block_entry(t):
        return t.record_size


class MDictWriter(MDictWriterBase):
    def _build_offset_table(self, d):
        """One key own multi entry, so d is list"""
        items = sorted(d, key=lambda x: locale.strxfrm(x['key']))

        self._offset_table = []
        offset = 0
        for record in items:
            key = record['key']
            key_enc = key.encode(self._python_encoding)
            key_null = (key + "\0").encode(self._python_encoding)
            key_len = len(key_enc) // self._encoding_length

            self._offset_table.append(_OffsetTableEntry(
                key0=record['key'],
                key=key_enc,
                key_null=key_null,
                key_len=key_len,
                record_null=record['path'],
                record_size=record['size'],
                record_pos=record['pos'],
                offset=offset,
                encoding=getattr(self, '_encoding', 'utf-8'),
                is_mdd=self._is_mdd,
            ))
            offset += record['size']
        self._total_record_len = offset

    def _build_record_blocks(self):
        self._record_blocks = self._split_blocks(_MdxRecordBlock)

    def _build_recordb_index(self):
        pass

    def _write_record_sect(self, outfile, callback=None):
        # outfile: a file-like object, opened in binary mode.
        if self._version == "2.0":
            record_format = b">QQQQ"
            index_format = b">QQ"
        else:
            record_format = b">LLLL"
            index_format = b">LL"
        # fill ZERO
        record_pos = outfile.tell()
        outfile.write(struct.pack(record_format, 0, 0, 0, 0))
        outfile.write((struct.pack(index_format, 0, 0)) * len(self._record_blocks))

        recordblocks_total_size = 0
        recordb_index = []
        for b in self._record_blocks:
            b.prepare()
            recordblocks_total_size += len(b.get_block())
            recordb_index.append(b.get_index_entry())
            outfile.write(b.get_block())
            callback and callback(len(b._offset_table))
            b.clean()
        end_pos = outfile.tell()
        self._recordb_index = b''.join(recordb_index)
        self._recordb_index_size = len(self._recordb_index)
        # fill REAL value
        outfile.seek(record_pos)
        outfile.write(struct.pack(record_format,
                                  len(self._record_blocks),
                                  self._num_entries,
                                  self._recordb_index_size,
                                  recordblocks_total_size))
        outfile.write(self._recordb_index)
        outfile.seek(end_pos)

    def write(self, outfile, callback=None):
        self._write_header(outfile)
        self._write_key_sect(outfile)
        self._write_record_sect(outfile, callback=callback)


def pack(target, dictionary, title='', description='', encoding='utf-8', is_mdd=False):
    def callback(value):
        bar.update(value)

    writer = MDictWriter(dictionary, title=title, description=description, encoding=encoding, is_mdd=is_mdd)
    bar = tqdm(total=len(writer._offset_table), unit='rec')
    outfile = open(target, "wb")
    writer.write(outfile, callback=callback)
    outfile.close()
    bar.close()


def txt2sqlite(source, callback=None):
    db_name = source + '.db'
    conn = sqlite3.connect(db_name)
    c = conn.cursor()
    c.execute('DROP TABLE IF EXISTS mdx_txt')
    c.execute('CREATE TABLE mdx_txt (entry text not null, paraphrase text not null)')
    max_batch = 1024 * 10
    with open(source, 'rt') as f:
        count = 0
        entries = []
        key = None
        content = []
        for line in f:
            line = line.strip()
            if not line:
                continue
            if line == '</>':
                content = ''.join(content)
                entries.append((key, content))
                if count > max_batch:
                    c.executemany('INSERT INTO mdx_txt VALUES (?,?)', entries)
                    conn.commit()
                    count = 0
                    entries = []
                key = None
                content = []
                callback and callback(1)
            elif not key:
                key = line
                count += 1
            else:
                content.append(line)
        if entries:
            c.executemany('INSERT INTO mdx_txt VALUES (?,?)', entries)
            conn.commit()
        c.execute('CREATE INDEX entry_index ON mdx_txt (entry)')
        conn.close()


def sqlite2txt(source, callback=None):
    mdx_txt = source + '.txt'
    with open(mdx_txt, 'wt') as f:
        sql = 'SELECT entry, paraphrase FROM mdx_txt'
        with sqlite3.connect(source) as conn:
            cur = conn.execute(sql)
            for c in cur:
                f.write(c[0] + '\r\n')
                f.write(c[1] + '\r\n')
                f.write('</>\r\n')
                callback and callback(1)


def pack_mdx_sqlite3(source, encoding='utf-8', callback=None):
    dictionary = []
    sql = 'SELECT entry, paraphrase FROM mdx_txt'
    with sqlite3.connect(source) as conn:
        cur = conn.execute(sql)
        for c in cur:
            dictionary.append({
                'key': c[0],
                'pos': 0,
                'path': source,
                'size': len((c[1] + '\0').encode(encoding)),
            })
            callback and callback(1)
    return dictionary


def pack_mdx_txt(source, encoding='utf-8', callback=None):
    dictionary = []
    with open(source, 'rb') as f:
        key = None
        content = []
        pos = 0
        line = f.readline()
        while line:
            line = line.strip()
            if not line:
                line = f.readline()
                continue
            if line == b'</>':
                content = b''.join(content)
                size = len(content + b'\0')
                dictionary.append({
                    'key': key.decode(encoding),
                    'pos': pos,
                    'path': source,
                    'size': size,
                })
                key = None
                content = []
                callback and callback(1)
            elif not key:
                key = line
                pos = f.tell()
            else:
                content.append(line)

            line = f.readline()
    return dictionary


def pack_mdd_file(source, callback=None):
    dictionary = []
    source = os.path.abspath(source)
    if os.path.isfile(source):
        size = os.path.getsize(source)
        key = '\\' + os.path.basename(source)
        if os.sep != '\\':
            key = key.replace(os.sep, '\\')
        dictionary.append({
            'key': key,
            'pos': 0,
            'path': source,
            'size': size,
        })
    else:
        relpath = source
        for root, dirs, files in os.walk(source):
            for f in files:
                fpath = os.path.join(root, f)
                size = os.path.getsize(fpath)
                key = '\\' + os.path.relpath(fpath, relpath)
                if os.sep != '\\':
                    key = key.replace(os.sep, '\\')
                dictionary.append({
                    'key': key,
                    'pos': 0,
                    'path': fpath,
                    'size': size,
                })
                callback and callback(1)
    return dictionary