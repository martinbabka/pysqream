'''                     ----  SQream Native Python API  ----

'''

import socket, json, ssl, logging, time, traceback
from struct import pack, pack_into, unpack, error as struct_error
from datetime import datetime, date, time as t
from multiprocessing import Pool, Manager
from mmap import mmap
from functools import reduce
from concurrent.futures import ProcessPoolExecutor
try:
    import cython
    CYTHON = True
except:
    CYTHON = False

try:
    import pyarrow as pa
    from pyarrow import csv
    import numpy as np
    ARROW = True
except:
    ARROW = False
else:
    sqream_to_pa = {
        'ftBool':     pa.bool_(),
        'ftUByte':    pa.uint8(),
        'ftShort':    pa.int16(),
        'ftInt':      pa.int32(),
        'ftLong':     pa.int64(),
        'ftFloat':    pa.float32(),
        'ftDouble':   pa.float64(),
        'ftDate':     pa.timestamp('ns'),
        'ftDateTime': pa.timestamp('ns'),
        'ftVarchar':  pa.string(),
        'ftBlob':     pa.utf8()
    }


__version__ = '3.0.0'

PROTOCOL_VERSION = 7
BUFFER_SIZE = 100 * int(1e6)  # For setting auto-flushing on netrwork insert
ROWS_PER_FLUSH = 100000
DEFAULT_CHUNKSIZE = 0  # Dummy variable for some jsons
FETCH_MANY_DEFAULT = 1  # default parameter for fetchmany()
VARCHAR_ENCODING = 'ascii'

# For encoding data to be sent to SQream using struct.pack() and for type checking by _set_val()
type_to_letter = {
    'ftBool': '?',
    'ftUByte': 'B',
    'ftShort': 'h',
    'ftInt': 'i',
    'ftLong': 'q',
    'ftFloat': 'f',
    'ftDouble': 'd',
    'ftDate': 'i',
    'ftDateTime': 'q',
    'ftVarchar': 's',
    'ftBlob': 's'
}

## Setup Logging and debug prings
## ------------------------------
dbg = False
clean_sqream_errors = False

def printdbg(*debug_print):
    if dbg:
        print(*debug_print)


## Date and Datetime conversion functions
#  --------------------------------------
'''
  SQream uses a dedicated algorithm to store dates as ints and datetimes as longs. The algorithm is depicted here:
  https://alcor.concordia.ca/~gpkatch/gdate-algorithm.html
  The logic behind it is explained here:
  https:#alcor.concordia.ca/~gpkatch/gdate-method.html   
'''

def pad_dates(num):
    return ('0' if num < 10 else '') + str(num)


def sq_date_to_tuple(sqream_date, date_convert_func=date):

    if sqream_date is None:
        return None

    year = (10000 * sqream_date + 14780) // 3652425
    intermed_1 = 365 * year + year // 4 - year // 100 + year // 400
    intermed_2 = sqream_date - intermed_1
    if intermed_2 < 0:
        year = year - 1
        intermed_2 = sqream_date - (365 * year + year // 4 - year // 100 +
                                    year // 400)
    intermed_3 = (100 * intermed_2 + 52) // 3060

    year = year + (intermed_3 + 2) // 12
    month = int((intermed_3 + 2) % 12) + 1
    day = int(intermed_2 - (intermed_3 * 306 + 5) // 10 + 1)

    return date_convert_func(year, month, day)


def sq_datetime_to_tuple(sqream_datetime, dt_convert_func=datetime):
    ''' Getting the datetime items involves breaking the long into the date int and time it holds
        The date is extracted in the above, while the time is extracted here  '''

    if sqream_datetime is None:
        return None

    date_part = sqream_datetime >> 32
    time_part = sqream_datetime & 0xffffffff
    date_part = sq_date_to_tuple(date_part)

    msec = time_part % 1000
    sec = (time_part // 1000) % 60
    mins = (time_part // 1000 // 60) % 60
    hour = time_part // 1000 // 60 // 60

    return dt_convert_func(date_part.year, date_part.month, date_part.day,
                           hour, mins, sec, msec)


def date_tuple_to_int(year: int, month: int, day: int) -> int:

    mth: int = (month + 9) % 12
    yr: int = year - mth // 10
    return 365 * yr + yr // 4 - yr // 100 + yr // 400 + (mth * 306 +
                                                         5) // 10 + (day - 1)


def datetime_tuple_to_long(year: int, month: int, day: int, hour: int, minute: int, second: int, msecond: int = 0) -> int:
    ''' self contained to avoid function calling overhead '''

    mth: int = (month + 9) % 12
    yr: int = year - mth // 10
    date_int: int = 365 * yr + yr // 4 - yr // 100 + yr // 400 + (
        mth * 306 + 5) // 10 + (day - 1)
    time_int: int = hour * 3600 * 1000 + minute * 60 * 1000 + second * 1000 + msecond // 1000

    return (date_int << 32) + time_int


def lengths_to_pairs(nvarc_lengths):
    ''' Accumulative sum generator, used for parsing nvarchar columns '''

    idx = new_idx = 0
    for length in nvarc_lengths:
        new_idx += length
        yield idx, new_idx
        idx = new_idx



def numpy_datetime_str_to_tup(numpy_dt):
    ''' '1970-01-01T00:00:00.699148800' '''

    numpy_dt = repr(numpy_dt).split("'")[1]
    date_part, time_part = numpy_dt.split('T')
    year, month, day = date_part.split('-')
    hms, ns = time_part.split('.')
    hour, mins, sec = hms.split(':')


    return year, month, day, hour, mins, sec, ns



def numpy_datetime_str_to_tup2(numpy_dt):
    ''' '1970-01-01T00:00:00.699148800' '''

    ts = (numpy_dt - np.datetime64('1970-01-01T00:00:00Z')) / np.timedelta64(1, 's') 
    dt = datetime.utcfromtimestamp(ts) 

    return dt.year, dt.month, dt.day


    return year, month, day, hour, mins, sec, ns



## Socket related
#  --------------


class SQSocket:
    ''' Extended socket class with some'''
    
    def __init__(self, ip, port, use_ssl=False):
        self.ip, self.port, self.use_ssl = ip, port, use_ssl
        self._setup_socket(ip, port)
   
    
    def _setup_socket(self, ip, port):

        self.s = socket.socket()
        if self.use_ssl:
            self.s = ssl.wrap_socket(self.s)

        try:
            self.timeout(10)
            self.s.connect((ip, port))
        except ConnectionRefusedError as e:
            raise ConnectionRefusedError("Connection refused, perhaps wrong IP?")
        except ConnectionResetError:
            raise Exception('Trying to connect to an SSL port with use_ssl = False')
        except Exception as e:
            if 'timeout' in repr(e):
                raise Exception ("Timeout when connecting to SQream, perhaps wrong IP?")
            elif '[SSL: UNKNOWN_PROTOCOL] unknown protocol' in repr(e):
                 raise Exception('Using use_ssl=True but connected to non ssl sqreamd port')
            else:
                raise Exception(e)
        else:
            self.timeout(None)

    # General socket / tls socket functionality
    #

    def _check_server_up(self, ip = None, port = None, use_ssl = None):

        try:
            SQSocket(ip or self.ip, port or self.port, use_ssl or self.use_ssl)
        except ConnectionRefusedError:
            raise ConnectionRefusedError("Connection to SQream interrupted")


    def send(self, data):

        # print ("sending: ", data)
        #try:
        return self.s.send(data)
        
        #except BrokenPipeError:
        #    raise BrokenPipeError('No connection to SQream. Try reconnecting')


    def close(self):

        return self.s.close()


    def timeout(self, timeout = 'not passed'):

        if timeout == 'not passed':
            return self.s.gettimeout()

        self.s.settimeout(timeout)


    # Extended functionality
    #

    def reconnect(self, ip = None, port = None):

        self.s.close()
        self._setup_socket(ip or self.ip, port or self.port)


    def receive(self, byte_num, timeout=None):
        ''' Read a specific amount of bytes from a given socket '''

        data = bytearray(byte_num)
        view = memoryview(data)
        total = 0

        if timeout:
            self.s.settimeout(timeout)
        
        while view:
            # Get whatever the socket gives and put it inside the bytearray
            received = self.s.recv_into(view)
            if received == 0:
                raise ConnectionRefusedError('SQreamd connection interrupted - 0 returned by socket')
            view = view[received:]
            total += received

        if timeout:
            self.s.settimeout(None)
            
        
        return data


    def get_response(self, is_text_msg=True):
        ''' Get answer JSON string from SQream after sending a relevant message '''

        # Getting 10-byte response header back
        header = self.receive(10)
        server_protocol = header[0]
        if server_protocol not in (6, 7):
            raise Exception(
                f'Protocol mismatch, client version - {PROTOCOL_VERSION}, server version - {server_protocol}'
            )
        # bytes_or_text =  header[1]
        message_len = unpack('q', header[2:10])[0]

        return self.receive(message_len).decode(
            'utf8') if is_text_msg else self.receive(message_len)

    
    # Non socket aux. functionality
    #

    def generate_message_header(self, data_length, is_text_msg=True, protocol_version=PROTOCOL_VERSION):
        ''' Generate SQream's 10 byte header prepended to any message '''

        return pack('bb', protocol_version, 1 if is_text_msg else 2) + pack(
            'q', data_length)


    def validate_response(self, response, expected):

        if expected not in response:
            # Color first line of SQream error (before the haskell thingy starts) in Red
            response = '\033[31m' + (response.split('\\n')[0] if clean_sqream_errors else response) + '\033[0m' 
            raise Exception(f'\nexpected response {expected} but got:\n\n {response}')

    


## Buffer setup and functionality
#  ------------------------------

manager = Manager()
buf_maps, buf_views = [], []


class ColumnBuffer:
    ''' Buffer holding packed columns to be sent to SQream '''

    def __init__(self, size=BUFFER_SIZE):
        global buf_maps, buf_views
        

    def clear(self):
        if buf_maps:
            [buf_map.close() for buf_map in buf_maps[0]]


    def init_buffers(self, col_sizes, col_nul):

        self.clear()
        buf_maps = [mmap(-1, ((1 if col_nul else 0)+(size if size!=0 else 104)) * ROWS_PER_FLUSH) for size in col_sizes]
        buf_views = [memoryview(buf_map) for buf_map in buf_maps]
        self.pool = Pool()

    
    def pack_columns(self, cols, capacity, col_types, col_sizes, col_nul, col_tvc):
        ''' Packs the buffer starting a given index with the column. 
            Returns number of bytes packed '''

        pool_params = zip(cols, range(len(col_types)), col_types,
                          col_sizes, col_nul, col_tvc)
        # To use multiprocess type packing, we call a top level function with a single tuple parameter
        try:
            packed_cols = self.pool.map(_pack_column, pool_params)  # buf_end_indices
        except Exception as e:
            printdbg("Original error from pool.map: ", e)
            raise ProgrammingError(
                "Error packing columns. Check that all types match the respective column types"
            )

        return packed_cols


    def close(self):
        self.clear()
        self.pool.close()
        self.pool.join()



## A top level packing function for Python's MP compatibility
def _pack_column(col_tup, return_actual_data = True):
    ''' Packs the buffer starting a given index with the column. 
        Returns number of bytes packed '''

    col, col_idx, col_type, size, nullable, tvc = col_tup
    capacity = len(col)
    buf_idx = 0
    buf_map =  mmap(-1, ((1 if nullable else 0)+(size if size!=0 else 104)) * ROWS_PER_FLUSH)
    buf_view = memoryview(buf_map) 

    def pack_exception(e):
        ''' Allowing to return traceback info from parent process when using mp.Pool on _pack_column
            [add link]
        '''
        e.traceback = traceback.format_exc()
        raise ProgrammingError(
            f'Trying to insert unsuitable types to column number {col_idx + 1} of type {col_type}'
        )

    # Numpy array for column
    if ARROW and isinstance(col, np.ndarray):
        # Pack null column if applicable
        if nullable:
            pack_into(f'{capacity}b', buf_view, buf_idx,
                      *[1 if item in (np.nan, b'') else 0 for item in col])
            buf_idx += capacity

            # Replace Nones with appropriate placeholder
            if 'S' in repr(col.dtype):    # already b''?
                pass
            elif 'U' in repr(col.dtype):
                pass
            else:
                # Swap out the nans
                col[col == np.nan] = 0

        # Pack nvarchar length column if applicable
        if tvc:
            buf_map.seek(buf_idx)
            lengths_as_bytes = np.vectorize(len)(col).astype('int32').tobytes()
            buf_map.write(lengths_as_bytes)
            buf_idx += len(lengths_as_bytes)

        # Pack the actual data
        if 'U' in repr(col.dtype):
            packed_strings = ''.join(col).encode('utf8')
            buf_map.seek(buf_idx)
            buf_map.write(packed_strings)
            buf_idx += len(packed_strings)
            print (f'unicode strings: {packed_strings}')
        else:
            packed_np = col.tobytes()
            buf_map.seek(buf_idx)
            buf_map.write(packed_np)
            buf_idx += len(packed_np)


        return buf_map[0:buf_idx] if return_actual_data else (0, buf_idx)


    # Pack null column if applicable
    type_code = type_to_letter[col_type]

    # If a text column, replace and pack in adavnce to see if the buffer is sufficient
    if col_type == 'ftBlob':
        try:
            encoded_col = [strn.encode('utf8') if strn is not None else b''
                for strn in col
            ]
        except AttributeError as e:  # Non strings will not have .encode()
            pack_exception(e)
        else:
            packed_strings = b''.join(encoded_col)

        needed_buf_size = len(packed_strings) + 5* capacity
        
        # Resize the buffer if not enough space for the current strings
        if needed_buf_size > len(buf_map):
            buf_view.release()
            buf_map.resize(needed_buf_size)
            buf_view = memoryview(buf_map)

    if nullable:
        pack_into(f'{capacity}b', buf_view, buf_idx,
                  *[1 if item is None else 0 for item in col])
        buf_idx += capacity


    # Replace Nones with appropriate placeholder - this affects the data itself
    if col_type == 'ftBlob':
        pass     # Handled preemptively due to allow possible buffer resizing

    elif col_type == 'ftVarchar':
        try:
            col = (strn.encode(VARCHAR_ENCODING)[:size].ljust(size, b' ')
                   if strn is not None else b''.ljust(size, b' ')
                   for strn in col)
        except AttributeError as e:  # Non strings will not have .encode()
            pack_exception(e)
        else:
            packed_strings = b''.join(col)

    elif col_type == 'ftDate':
        try:
            col = (date_tuple_to_int(*deit.timetuple()[:3])
                   if deit is not None else date_tuple_to_int(1900, 1, 1)
                   for deit in col)
        except AttributeError as e:  # Non date/times will not have .timetuple()
            pack_exception(e)

    elif col_type == 'ftDateTime':
        try:
            col = (datetime_tuple_to_long(*(dt.timetuple()[:6] + (dt.microsecond, )))
                   if dt is not None else datetime_tuple_to_long(
                       1900, 1, 1, 0, 0, 0) for dt in col
                   )
        except AttributeError as e:
            pack_exception(e)

    elif col_type in ('ftBool', 'ftUByte', 'ftShort', 'ftInt', 'ftLong',
                      'ftFloat', 'ftDouble'):
        col = (num if num is not None else 0 for num in col)

    else:
        raise ProgrammingError(f'Bad column type passed: {col_type}')


    # Pack nvarchar length column if applicable
    if tvc:
        pack_into(f'{capacity}i', buf_view, buf_idx,
                  *[len(string) for string in encoded_col])
        buf_idx += 4 * capacity


    # Done preceding column handling, pack the actual data
    if col_type in ('ftVarchar', 'ftBlob'):
        buf_map.seek(buf_idx)
        buf_map.write(packed_strings)
        buf_idx += len(packed_strings)
    else:
        try:
            pack_into(f'{capacity}{type_code}', buf_view, buf_idx, *col)
        except struct_error as e:
            pack_exception(e)

        buf_idx += capacity * size

    return buf_map[0:buf_idx] if return_actual_data else (0, buf_idx)


class Connection:
    ''' Connection class used to interact with SQream '''

    base_conn_open = [False]

    def __init__(self, ip, port, clustered, use_ssl=False, base_connection=True, reconnect_attempts=3, reconnect_interval=10):

        self.buffer = ColumnBuffer(BUFFER_SIZE)  # flushing buffer every BUFFER_SIZE bytes
        self.row_size = 0
        self.rows_per_flush = 0
        self.stmt_id = None  # For error handling when called out of order
        self.open_statement = False
        self.closed = False
        self.orig_ip, self.orig_port, self.clustered, self.use_ssl = ip, port, clustered, use_ssl
        self.reconnect_attempts, self.reconnect_interval = reconnect_attempts, reconnect_interval
        self.base_connection = base_connection
        self._open_connection(clustered, use_ssl)
        self.arraysize = FETCH_MANY_DEFAULT
        self.rowcount = -1    # DB-API property
        self.more_to_fetch = None

        if self.base_connection:
            self.cursors = []


    ## SQream mechanisms
    #  -----------------

    def _open_connection(self, clustered, use_ssl):
        ''' Get proper ip and port from picker if needed and connect socket. Used at __init__() '''
        
        if clustered:
            # Create non SSL socket for picker communication
            picker_socket = SQSocket(self.orig_ip, self.orig_port, False)
            # Parse picker response to get ip and port
            # Read 4 bytes to find length of how much to read
            picker_socket.timeout(5)
            try:
                read_len = unpack('i', picker_socket.receive(4))[0]
            except socket.timeout:
                raise ProgrammingError(f'Connected with clustered=True, but apparently not a server picker port')
            picker_socket.timeout(None)

            # Read the number of bytes, which is the IP in string format
            # Using a nonblocking socket in case clustered = True was passed but not connected to picker
            self.ip = picker_socket.receive(read_len)

            # Now read port
            self.port = unpack('i', picker_socket.receive(4))[0]
            picker_socket.close()
        else:
            self.ip, self.port = self.orig_ip, self.orig_port

        # Create socket and connect to actual SQreamd server
        self.s = SQSocket(self.ip, self.port, use_ssl)
        if self.base_connection:
            self.base_conn_open[0] = True

    

    def _send_string(self, json_cmd, get_response=True, is_text_msg=True, sock=None):
        ''' Encode a JSON string and send to SQream. Optionally get response '''

        # Generating the message header, and sending both over the socket
        self.s.send(self.s.generate_message_header(len(json_cmd)) + json_cmd.encode('utf8'))

        if get_response:
            return self.s.get_response(is_text_msg)
                

    def connect_database(self, database, username, password, service='sqream'):
        ''' Handle connection to database, with or without server picker '''

        self.database, self.username, self.password, self.service = database, username, password, service
        res = self._send_string(
            f'{{"username":"{username}", "password":"{password}", "connectDatabase":"{database}", "service":"{service}"}}'
        )
        res = json.loads(res)
        try:
            self.connection_id = res['connectionId']
        except KeyError as e:
            raise ProgrammingError('Error connecting to database: ', res['error'])

        self.varchar_enc = res.get('varcharEncoding', 'ascii')


    def _attempt_reconnect(self):

        for attempt in range(self.reconnect_attempts):
            time.sleep(self.reconnect_interval)
            try:
                self._open_connection(self.clustered, self.use_ssl)
                self.connect_database(self.database, self.username, self.password, self.service)
            except ConnectionRefusedError as e:
                print(f'Connection lost, retry attempt no. {attempt+1} failed. Original error:\n{e}')
            else:
                return

        # Attempts failed
        raise ConnectionRefusedError('Reconnection attempts to sqreamd failed')


    def execute_sqream_statement(self, stmt):
        ''' '''

        self.latest_stmt = stmt

        if self.open_statement:
            self.close_statement()
        self.open_statement = True

        self.more_to_fetch = True    

        self.stmt_id = json.loads(self._send_string('{"getStatementId" : "getStatementId"}'))["statementId"]
        
        stmt = json.dumps({"prepareStatement": stmt, "chunkSize": DEFAULT_CHUNKSIZE})
        res = self._send_string(stmt)

        self.s.validate_response(res, "statementPrepared")
        self.lb_params = json.loads(res)
        if self.lb_params.get('reconnect'):  # Reconnect exists and issued, otherwise False / None

            # Close socket, open a new socket with new port/ip sent be the reconnect response
            self.s.reconnect(
                self.lb_params['ip'], self.lb_params['port_ssl']
                if self.use_ssl else self.lb_params['port'])

            # Send reconnect and reconstruct messages
            reconnect_str = '{{"service": "{}", "reconnectDatabase":"{}", "connectionId":{}, "listenerId":{},"username":"{}", "password":"{}"}}'.format(
                self.service, self.database, self.connection_id,
                self.lb_params['listener_id'], self.username, self.password)
            self._send_string(reconnect_str)
            self._send_string('{{"reconstructStatement": {}}}'.format(
                self.stmt_id))

        # Reconnected/reconstructed if needed,  send  execute command
        self._send_string('{"execute" : "execute"}')

        # Send queryType message/s
        res = json.loads(self._send_string('{"queryTypeIn": "queryTypeIn"}'))
        self.column_list = res.get('queryType', '')

        if not self.column_list:
            res = json.loads(
                self._send_string('{"queryTypeOut" : "queryTypeOut"}'))
            self.column_list = res.get('queryTypeNamed', '')
            if not self.column_list:
                self.statement_type = 'DML'
                self.close_statement()
                return

            self.statement_type = 'SELECT' if self.column_list else 'DML'
            self.result_rows = []
            self.unparsed_row_amount = 0
            self.data_columns = []
        else:
            self.statement_type = 'INSERT'

        # {"isTrueVarChar":false,"nullable":true,"type":["ftInt",4,0]}
        self.col_names, self.col_tvc, self.col_nul, self.col_type_tups = \
            list(zip(*[(col.get("name", ""), col["isTrueVarChar"], col["nullable"], col["type"]) for col in self.column_list]))
        self.col_names_map = {
            name: idx
            for idx, name in enumerate(self.col_names)
        }

        if self.statement_type == 'INSERT':
            self.col_types = [type_tup[0] for type_tup in self.col_type_tups]
            self.col_sizes = [type_tup[1] for type_tup in self.col_type_tups]
            self.row_size = sum(self.col_sizes) + sum(
                self.col_nul) + 4 * sum(self.col_tvc)
            self.rows_per_flush = ROWS_PER_FLUSH
            self.buffer.init_buffers(self.col_sizes, self.col_nul)

        # if self.statement_type == 'SELECT':
        self.parsed_rows = []
        self.parsed_row_amount = 0
    

    ## Select

    def _fetch(self, sock=None):

        sock = sock or self.s
        # JSON correspondence
        res = json.loads(self._send_string('{"fetch" : "fetch"}'))
        num_rows_fetched, column_sizes = res['rows'], res['colSzs']

        if num_rows_fetched == 0:
            self.close_statement()
            return num_rows_fetched

        # Get preceding header
        self.s.receive(10)

        # Get data as memoryviews of bytearrays.
        unsorted_data_columns = [
            memoryview(self.s.receive(size))
            for idx, size in enumerate(column_sizes)
        ]

        # Sort by columns, taking a memoryview and casting to the proper type
        self.data_columns = []

        for type_tup, nullable, tvc in zip(self.col_type_tups, self.col_nul,
                                           self.col_tvc):
            column = []
            if nullable:
                column.append(unsorted_data_columns.pop(0))
            if tvc:
                column.append(unsorted_data_columns.pop(0).cast('i'))

            column.append(unsorted_data_columns.pop(0))

            if type_tup[0] not in ('ftVarchar', 'ftBlob'):
                column[-1] = column[-1].cast(type_to_letter[type_tup[0]])
            else:
                column[-1] = column[-1].tobytes()
            self.data_columns.append(column)

        self.unparsed_row_amount = num_rows_fetched

        return num_rows_fetched

    def _parse_fetched_cols(self):
        ''' Used by _fetch_and_parse ()  '''

        self.extracted_cols = []

        if not self.data_columns:
            return self.extracted_cols

        for idx, raw_col_data in enumerate(self.data_columns):
            # Extract data according to column type
            if self.col_tvc[idx]:  # nvarchar
                nvarc_sizes = raw_col_data[1]
                col = [
                    raw_col_data[-1][start:end].decode('utf8')
                    for (start, end) in lengths_to_pairs(nvarc_sizes)
                ]
            elif self.col_type_tups[idx][0] == "ftVarchar":
                varchar_size = self.col_type_tups[idx][1]
                col = [
                    raw_col_data[-1][idx:idx + varchar_size].decode(
                        self.varchar_enc).rstrip('\x00').rstrip()
                    for idx in range(0, len(raw_col_data[-1]), varchar_size)
                ]
            elif self.col_type_tups[idx][0] == "ftDate":
                col = [sq_date_to_tuple(d) for d in raw_col_data[-1]]
            elif self.col_type_tups[idx][0] == "ftDateTime":
                col = [sq_datetime_to_tuple(d) for d in raw_col_data[-1]]

            else:
                col = raw_col_data[-1]

            # Fill Nones if / where needed
            if self.col_nul[idx]:
                nulls = raw_col_data[0]  # .tolist()
                col = [
                    item if not null else None
                    for item, null in zip(col, nulls)
                ]
            else:
                pass

            self.extracted_cols.append(col)

        # Done with the raw data buffers
        self.unparsed_row_amount = 0
        self.data_columns = []

        return self.extracted_cols

    def _fetch_and_parse(self, requested_row_amount, data_as='rows'):
        ''' See if this amount of data is available or a fetch from sqream is required 
            -1 - fetch all available data. Used by fetchmany() '''

        if data_as == 'rows':
            while (requested_row_amount > len(self.parsed_rows)
                   or requested_row_amount == -1) and self.more_to_fetch:
                self.more_to_fetch = bool(self._fetch())  # _fetch() updates self.unparsed_row_amount

                self.parsed_rows.extend(zip(*self._parse_fetched_cols()))


    ## Insert

    def _send_columns(self, cols=None, capacity=None):
        ''' Perform network insert - "put" json, header, binarized columns. Used by executemany() '''

        cols = cols or self.cols
        cols = cols if isinstance(cols, (list, tuple, set, dict)) else list(cols)

        capacity = capacity or self.capacity

        # Send columns and metadata to be packed into our buffer
        packed_cols = self.buffer.pack_columns(cols, capacity, self.col_types,
                                               self.col_sizes, self.col_nul,
                                               self.col_tvc)

        byte_count = sum(len(packed_col) for packed_col in packed_cols)

        # Sending put message and binary header
        self._send_string(f'{{"put":{capacity}}}', False)
        self.s.send((self.s.generate_message_header(byte_count, False)))

        # Sending packed data (binary buffer)
        for packed_col in packed_cols:
            self.s.send((packed_col))

        self.s.validate_response(self.s.get_response(), '{"putted":"putted"}')


    ## Closing

    def close_statement(self, sock=None):

        sock = sock or self.s
        self._send_string('{"closeStatement": "closeStatement"}')
        self.open_statement = False

    def close_connection(self, sock=None):

        if self.closed:
            raise ProgrammingError(
                "Trying to close a connection that's already closed")
        
        self._send_string('{"closeConnection":  "closeConnection"}')
        self.s.close()
        self.buffer.close()
        self.closed = True
        self.base_conn_open[0] = False if self.base_connection else True  


    # '''
    def csv_to_table(self, csv_path, table_name, read = None, parse = None, convert = None, con = None, auto_infer = False):
        ' Pyarrow CSV reader documentation: https://arrow.apache.org/docs/python/generated/pyarrow.csv.read_csv.html '

        if not ARROW:
            return "Optional pyarrow dependency not found. To install: pip3 install pyarrow"
        
        sqream_to_pa = {
            'ftBool':     pa.bool_(),
            'ftUByte':    pa.uint8(),
            'ftShort':    pa.int16(),
            'ftInt':      pa.int32(),
            'ftLong':     pa.int64(),
            'ftFloat':    pa.float32(),
            'ftDouble':   pa.float64(),
            'ftDate':     pa.timestamp('ns'),
            'ftDateTime': pa.timestamp('ns'),
            'ftVarchar':  pa.string(),
            'ftBlob':     pa.utf8()
        }

        start = time.time()
        # Get table metadata
        con = con or self
        con.execute(f'select * from {table_name} where 1=0')
        
        # Map column names to pyarrow types and set Arrow's CSV parameters
        sqream_col_types = [col_type[0] for col_type in con.col_type_tups]
        column_types = zip(con.col_names, [sqream_to_pa[col_type[0]] for col_type in con.col_type_tups])
        read = read or csv.ReadOptions(column_names=con.col_names)
        parse = parse or csv.ParseOptions(delimiter='|')
        convert = convert or csv.ConvertOptions(column_types = None if auto_infer else column_types)
        
        # Read CSV to in-memory arrow format
        csv_arrow = csv.read_csv(csv_path, read_options=read, parse_options=parse, convert_options=convert).combine_chunks()
        num_chunks = len(csv_arrow[0].chunks)
        numpy_cols = []

        # For each column, get the numpy representation for quick packing 
        for col_type, col in zip(sqream_col_types, csv_arrow):
            # Only one chunk after combine_chunks()
            col = col.chunks[0]
            if col_type in  ('ftVarchar', 'ftBlob', 'ftDate', 'ftDateTime'):
                col = col.to_pandas()
            else:
                col = col.to_numpy()
            
            numpy_cols.append(col)
        
        print (f'total loading csv: {time.time()-start}')
        start = time.time()
        
        # Insert columns into SQream
        col_num = csv_arrow.shape[1]
        con.executemany(f'insert into {table_name} values ({"?,"*(col_num-1)}?)', numpy_cols)
        print (f'total inserting csv: {time.time()-start}')

    # '''



    '''  -- Metadata  --
         ---------------   '''
    def get_statement_type(self):

        return self.statement_type

    def get_statement_id(self):

        return self.stmt_id

    
    ## DB-API API
    #  ----------

    def _verify_open(self):

        if not self.base_conn_open[0]:
            raise ProgrammingError('Connection has been closed')

        if self.closed:
            raise ProgrammingError('Cursor has been closed')

    def _verify_query_type(self, query_type):

        if not self.open_statement:
            raise ProgrammingError(
                'No open statement while attempting fetch operation')


    def _fill_description(self):
        '''Getting parameters for the cursor's 'description' attribute, even for 
           a query that returns no rows. For each column, this includes:  
           (name, type_code, display_size, internal_size, precision, scale, null_ok) '''

        if self.statement_type != 'SELECT':
            self.description = None
            return self.description

        self.description = []
        for col_name, col_nullalbe, col_type_tup in zip(
                self.col_names, self.col_nul, self.col_type_tups):
            type_code = typecodes[
                col_type_tup[0]]  # Convert SQream type to DBAPI identifier
            display_size = internal_size = col_type_tup[
                1]  # Check if other size is available from API
            precision = None
            scale = None

            self.description.append(
                (col_name, type_code, display_size, internal_size, precision,
                 scale, col_nullalbe))

        return self.description

    def execute(self, query, params=None):
        ''' Execute a statement. Parameters are not supported '''

        self._verify_open()
        # print ("query executed: ", query)
        if params:
            raise ProgrammingError("Parametered queries not supported. \
                If this is an insert query, use executemany() with the data rows as the parameter")
            
            # query = reduce(lambda query, param: query.replace('?', param, 1), (query,) + params)
            self.execute_sqream_statement(
                f"select drop_saved_query('dbapi_query')")
            self.close_statement()
            printdbg(f"\n\nselect save_query('dbapi_query', $${query}$$)")
            # query = query.replace("'", '"')
            self.execute_sqream_statement(
                f"select save_query('dbapi_query', $${query}$$)")
            params_as_str = ', '.join((str(param) for param in params))
            self.execute_sqream_statement(
                f"select execute_saved_query('dbapi_query', {params_as_str})")
            printdbg(
                '\n\nsaved query:',
                f"select execute_saved_query('dbapi_query', {params_as_str})")
        else:
            self.execute_sqream_statement(query)

        self._fill_description()
        self.rows_fetched = 0
        self.rows_returned = 0

        return self

    def executemany(self, query, rows_or_cols=None, data_as='rows', amount=None):
        ''' Execute a statement, including parametered data insert '''

        self._verify_open()
        self.execute(query)

        if rows_or_cols is None:
            return self

        if 'numpy' in repr(type(rows_or_cols[0])):
            data_as = 'numpy'

        # Network insert starts here if data was passed
        if len(set(len(row_or_col) for row_or_col in rows_or_cols)) > 1: 
            raise ProgrammingError(
                "Incosistent data sequences passed for inserting. Please use rows/columns of consistent length"
            )
        if data_as == 'rows':
            self.capacity = amount or len(rows_or_cols)
            self.cols = list(zip(*rows_or_cols))
        else:
            self.cols = rows_or_cols
            self.capacity = len(self.cols)

        # Slice a chunk of columns and pass to _send_columns()
        while self.cols != [()]:
            col_chunk = [col[:self.rows_per_flush] for col in self.cols]
            if len(col_chunk[0]) == 0:
                break
            self._send_columns(col_chunk, len(col_chunk[0]))
            self.cols = [col[self.rows_per_flush:] for col in self.cols]

        self.close_statement()

        return self

    def fetchmany(self, size=None, data_as='rows'):
        ''' Fetch an amount of result rows '''

        size = size or self.arraysize
        self._verify_open()
        
        if self.more_to_fetch is False:
            # All data in select statement was fetched
            return []
        
        self._verify_query_type('SELECT')
     
        self._fetch_and_parse(size, data_as)

        # Get relevant part of parsed rows and reduce storage and counter
        if data_as == 'rows':
            res = self.parsed_rows[0:size if size != -1 else None]
            self.parsed_rows = self.parsed_rows[size:] if size != -1 else []

        # print ('------ fetch result:', res)
        
        return (res if res else []) if size != 1 else (res[0] if res else None)

    def fetchone(self, data_as='rows'):
        ''' Fetch one result row '''

        if data_as not in ('rows',):
            raise ProgrammingError("Bad argument to fetchone()")

        return self.fetchmany(1, data_as)

    def fetchall(self, data_as='rows'):
        ''' Fetch all result rows '''

        if data_as not in ('rows',):
            raise ProgrammingError("Bad argument to fetchall()")

        return self.fetchmany(-1, data_as)

    def cursor(self):
        ''' Return a new connection with the same parameters.
            We use a connection as the equivalent of a 'cursor' '''

        cur = Connection(
            self.picker_ip if self.clustered else self.ip,
            self.picker_port if self.clustered else self.port,
            self.clustered,
            self.use_ssl,
            base_connection=False
        )  # self is the calling connection instance, so cursor can trace back to pysqream
        cur.connect_database(self.database, self.username, self.password,
                             self.service)

        self.cursors.append(cur)

        return cur

    def commit(self):
        self._verify_open()

    def rollback(self):
        pass

    def close(self):

        if self.closed:
            raise ProgrammingError(
                "Trying to close a connection that's already closed")

        self.close_statement()
        self.close_connection()
        self.closed = True

    def nextset(self):
        ''' No multiple result sets so currently always returns None '''

        return None

    # DB-API Do nothing (for now) methods
    # -----------------------------------

    def setinputsizes(self, sizes):

        self._verify_open()

    def setoutputsize(self, size, column=None):

        self._verify_open()

    # Internal Methods
    # ----------------
    ''' Include: __enter__(), __exit__() - for using "with", 
                 __iter__ for use in for-in clause  '''

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, exc_traceback):
        self.close()

    def __iter__(self):
        for item in self.fetchall():
            yield item


## Top level functionality
#  -----------------------


def connect(host, port, database, username, password, clustered = False, use_ssl = False, service='sqream', reconnect_attempts=3, reconnect_interval=10):
    ''' Connect to SQream database '''

    if not isinstance(reconnect_attempts, int) or reconnect_attempts < 0:
        raise Exception(f'reconnect attempts should be a positive integer, got : {reconnect_attempts}')
    if not isinstance(reconnect_interval, int) or reconnect_attempts < 0:
        raise Exception(f'reconnect interval should be a positive integer, got : {reconnect_interval}')


    conn = Connection(host, port, clustered, use_ssl, base_connection=True, reconnect_attempts=reconnect_attempts, reconnect_interval=reconnect_interval)
    conn.connect_database(database, username, password, service)

    return conn


## DBapi compatibility
#  -------------------
''' To fully comply to Python's DB-API 2.0 database standard. Ignore when using internally '''

# Type objects and constructors required by the DB-API 2.0 standard
Binary = memoryview
Date = date
Time = t
Timestamp = datetime


class Error(Exception):
    pass

class Warning(Exception):
    pass

class InterfaceError(Error):
    pass


class DatabaseError(Error):
    pass


class DataError(DatabaseError):
    pass


class OperationalError(DatabaseError):
    pass


class IntegrityError(DatabaseError):
    pass


class InternalError(DatabaseError):
    pass


class ProgrammingError(DatabaseError):
    pass


class NotSupportedError(DatabaseError):
    pass


class _DBAPITypeObject:
    """DB-API type object which compares equal to all values passed to the constructor.
        https://www.python.org/dev/peps/pep-0249/#implementation-hints-for-module-authors
    """
    def __init__(self, *values):
        self.values = values

    def __eq__(self, other):
        return other in self.values


STRING = "STRING"
BINARY = _DBAPITypeObject("BYTES", "RECORD", "STRUCT")
NUMBER = _DBAPITypeObject("INTEGER", "INT64", "FLOAT", "FLOAT64", "NUMERIC",
                          "BOOLEAN", "BOOL")
DATETIME = _DBAPITypeObject("TIMESTAMP", "DATE", "TIME", "DATETIME")
ROWID = "ROWID"

typecodes = {
    'ftBool': 'NUMBER',
    'ftUByte': 'NUMBER',
    'ftInt': 'NUMBER',
    'ftShort': 'NUMBER',
    'ftLong': 'NUMBER',
    'ftDouble': 'NUMBER',
    'ftFloat': 'NUMBER',
    'ftDate': 'DATETIME',
    'ftDateTime': 'DATETIME',
    'ftVarchar': 'STRING',
    'ftBlob': 'STRING'
}


def DateFromTicks(ticks):
    return Date.fromtimestamp(ticks)


def TimeFromTicks(ticks):
    return Time(
        *time.localtime(ticks)[3:6]
    )  # localtime() returns a namedtuple, fields 3-5 are hr/min/sec


def TimestampFromTicks(ticks):
    return Timestamp.fromtimestamp(ticks)


# More DBApi globals
## Globals
apilevel = '2.0'  # Always 2.0, 1.0 is long deprecated

threadsafety = 1

paramstyle = 'qmark'

if __name__ == '__main__':

    print('PySqream DB-API connector, version no.: ', __version__)
