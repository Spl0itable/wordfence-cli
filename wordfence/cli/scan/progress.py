import re
import curses
import logging
import signal
import os
from typing import List, Optional, Deque
from logging import Handler
from collections import deque, namedtuple
from time import sleep

from wordfence.scanning.scanner import (ScanProgressUpdate, ScanMetrics,
                                        default_scan_finished_handler)
from ..banner.banner import get_welcome_banner
from ...util import timing
from ...util.unicode import filter_control_characters


class ProgressException(Exception):
    pass


_displays = []

METRIC_BOX_WIDTH = 39
"""
Hard-coded width of metric boxes

The actual width taken up will be the hard-coded value +2 to account for the
left and right borders. Each box on the same row will be separated by the
padding value as well.
"""


def reset_terminal() -> None:
    for display in _displays:
        display.end()


def resize_terminal(signalnum, frame) -> None:
    for display in _displays:
        display.queue_resize()


signal.signal(signal.SIGWINCH, resize_terminal)


Position = namedtuple('Position', ['y', 'x'])


class LayoutProperties:

    def __init__(
                self,
                lines: int,
                current_line: int,
                max_row_width: int
            ):
        self.lines = lines
        self.current_line = current_line
        self.max_row_width = max_row_width


class Box:

    def __init__(
                self,
                parent: Optional[curses.window] = None,
                border: bool = True,
                title: Optional[str] = None
            ):
        self.parent = parent
        self.border = border
        self.title = title
        self.window = None
        self.position = None
        self.last_size = None

    def _initialize_window(self, y: int = 0, x: int = 0) -> None:
        height, width = self.compute_size()
        if self.parent is None:
            self.window = curses.newwin(height, width, y, x)
        else:
            self.window = self.parent.subwin(height, width, y, x)
        self.position = Position(y, x)

    def set_position(self, y: int, x: int) -> None:
        if self.window is None:
            self._initialize_window(y, x)
        else:
            self.resize(1, 1)
            try:
                self.window.erase()
                self.window.mvderwin(y, x)
                self.window.mvwin(y, x)
                self.position = Position(y, x)
            except Exception as e:
                size = os.get_terminal_size()
                raise ValueError(
                        f"error moving window: y: {y}, x: {x}; "
                        f"height: {self.get_height()}; "
                        f"width: {self.get_width()}; "
                        f"lines: {size.lines}; "
                        f"columns: {size.columns}"
                    ) from e
            self.resize()

    def _require_window(self) -> None:
        if self.window is None:
            self._initialize_window()

    def compute_size(self) -> (int, int):
        height = self.get_height()
        width = self.get_width()
        if self.border:
            width += 2
            height += 2
        self.last_size = (height, width)
        return self.last_size

    def resize(
                self,
                lines: Optional[int] = None,
                cols: Optional[int] = None
            ) -> None:
        if self.window is None:
            return
        height, width = self.compute_size()
        if lines is not None:
            height = lines
        if cols is not None:
            width = cols
        self.window.erase()
        try:
            self.window.resize(height, width)
        except Exception:
            pass  # Ignore temporary errors during resizing
        self.update()

    def set_title(self, title: str) -> None:
        self.title = title

    def render(self) -> None:
        self._require_window()
        height, width = self.compute_size()
        if self.border:
            self.window.border()
        if self.title is not None:
            title_length = len(self.title)
            title_offset = 0
            if title_length < width:
                title_offset = int((width - title_length) / 2)
            try:
                self.window.addstr(0, title_offset, self.title)
            except Exception:
                pass  # Ignore temporary errors during resizing
        try:
            self.draw_content()
        except Exception:
            pass  # Ignore temporary errors during resizing

    def get_border_offset(self) -> int:
        return 1 if self.border else 0

    def draw_content(self) -> None:
        pass

    def update(self) -> None:
        self.render()
        self.window.syncup()
        self.window.noutrefresh()

    def resize_for_layout(self, properties: LayoutProperties) -> False:
        return False


class Metric:

    def __init__(self, label: str, value):
        self.label = label
        self.value = str(value)


class MetricBox(Box):

    def __init__(
                self,
                metrics: List[Metric],
                title: Optional[str] = None,
                parent: Optional[curses.window] = None
            ):
        self.metrics = metrics
        super().__init__(parent, title=title)

    def get_width(self) -> int:
        return METRIC_BOX_WIDTH

    def get_height(self) -> int:
        return len(self.metrics)

    def draw_content(self) -> None:
        offset = self.get_border_offset()
        width = self.get_width()
        for index, metric in enumerate(self.metrics):
            line = index + offset
            label = f'{metric.label}:'
            value_string = metric.value.rjust(width - len(label))
            self.window.addstr(line, offset, label + value_string)


class BannerBox(Box):

    def colorize(self, string):
       return f"{self.color}{string}{curses.color_pair(0)}"

    def __init__(self, banner, color=None, parent=None):
        self.banner = banner
        self.color = color
        super().__init__(parent, border=False)

    def get_width(self):
        return self.banner.column_count

    def get_height(self):
        return self.banner.row_count

    def draw_content(self):
       offset = self.get_border_offset()
       for index, row in enumerate(self.banner.rows):
            self.window.addstr(index + offset, offset, self.colorize(row))

    def colorize(self, string):
        self.window.attron(self.color)
        self.window.addstr(string)
        self.window.attroff(self.color)
        return ""


DEFAULT_MAX_MESSAGES = 512000


class LogBox(Box):
    def __init__(
        self,
        columns: int,
        lines: int,
        max_messages: int = 0,
        parent: Optional[curses.window] = None,
    ):
        self.columns = columns
        self.lines = lines
        self.messages = deque(maxlen=self._determine_max_messages(max_messages))
        self.cursor_position = None
        self.has_file_paths = False  # Track if there are lines with file paths
        self.scan_complete = False  # Track if the scan has completed
        super().__init__(parent, border=True)

    def _determine_max_messages(self, max_messages: int = 0) -> Optional[int]:
        if max_messages < 0:
            return None
        elif max_messages == 0:
            return max(self.lines, DEFAULT_MAX_MESSAGES)
        else:
            return max_messages

    def get_width(self):
        return self.columns

    def get_height(self):
        return self.lines

    def _map_messages_to_lines(self, offset: int) -> Deque[str]:
        lines = deque(maxlen=self.lines)
        remaining_lines = self.lines
        for message in reversed(self.messages):
            if remaining_lines == 0:
                break
            message_lines = []
            while len(message):
                if remaining_lines == 0:
                    break
                line = message[:self.columns]
                message = message[self.columns:]
                message_lines.append(line)
                remaining_lines -= 1
            for line in reversed(message_lines):
                lines.appendleft(line)
        return lines

    def draw_content(self) -> None:
        offset = self.get_border_offset()
        line_number = offset
        last_line_number = line_number
        last_line_length = 0
        has_file_paths = False  # Flag to track if there are lines with file paths

        # Define color pairs for cyan and yellow
        CYAN_TEXT = 3
        YELLOW_TEXT = 4
        curses.init_pair(CYAN_TEXT, curses.COLOR_CYAN, curses.COLOR_BLACK)
        curses.init_pair(YELLOW_TEXT, curses.COLOR_YELLOW, curses.COLOR_BLACK)

        for line in self._map_messages_to_lines(offset):
            last_line_number = line_number
            last_line_length = len(line)
            line = line.ljust(self.columns)
            try:
                # Split the line into the file path and the log message
                file_path, log_message = line.split(' "', 1)
                # Check if the file path starts with "/www/"
                if file_path.startswith('/www/'):
                    has_file_paths = True  # Set the flag to True if there's at least one file path
                    # Enable the color pair and bold attribute for the file path (yellow)
                    self.window.attron(curses.color_pair(YELLOW_TEXT) | curses.A_BOLD)
                    self.window.addstr(line_number + 1, offset, file_path)  # Increase line_number by 1 to start on a new line
                    self.window.attroff(curses.color_pair(YELLOW_TEXT) | curses.A_BOLD)
                    # Write the delimiter with default text color
                    self.window.addstr(' "', curses.color_pair(0))
                    # Write the log message
                    self.window.addstr(log_message)
                else:
                    # Write the whole line as is (without using cyan color)
                    self.window.addstr(line_number, offset, line)
            except ValueError:
                # Delimiter not found, write the line as is
                self.window.addstr(line_number, offset, line)
            line_number += 1

        if has_file_paths:
            # Write the "Possible malicious file(s) found:" message in cyan color
            message = "Possible malicious file(s) found:"
            self.window.attron(curses.color_pair(CYAN_TEXT) | curses.A_BOLD)
            self.window.addstr(offset, offset, message)
            self.window.attroff(curses.color_pair(CYAN_TEXT) | curses.A_BOLD)
            line_number += 1
        elif self.scan_complete:
            # Clear the area where the "No malware found :)" message would be displayed
            self.window.addstr(offset, offset, ' ' * self.columns)

        # Write the "No malware found :)" message in green color only if there are no file paths and the scan is complete
        if not has_file_paths and self.scan_complete:
            message = "No malware found (⊃｡•́‿•̀｡)⊃"
            GREEN_TEXT = 3
            BOLD_TEXT = curses.A_BOLD
            curses.init_pair(GREEN_TEXT, curses.COLOR_GREEN, curses.COLOR_BLACK)
            self.window.attron(curses.color_pair(GREEN_TEXT) | BOLD_TEXT)
            self.window.addstr(offset, offset, message)
            self.window.attroff(curses.color_pair(GREEN_TEXT) | BOLD_TEXT)
            line_number += 1

        self.cursor_offset = Position(last_line_number, last_line_length)

    def add_message(self, message: str) -> None:
        # Strip ANSI color codes from the message
        ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
        message = ansi_escape.sub('', message)

        filtered_message = filter_control_characters(message)
        self.messages.append(filtered_message)
        if '/www/' in filtered_message:
            self.has_file_paths = True
        if 'Scan complete' in filtered_message:
            self.scan_complete = True
        self.update()

    def get_cursor_position(self) -> Position:
        y = 0
        x = 0
        if self.position is not None:
            y += self.position.y
            x += self.position.x
        if self.cursor_offset is not None:
            y += self.cursor_offset.y
            x += self.cursor_offset.x
        return Position(y, x)

    def resize_for_layout(self, properties: LayoutProperties) -> bool:
        self.columns = properties.max_row_width - 2
        self.lines = properties.lines - properties.current_line - 2
        self.cursor_position = None
        if self.lines < 3:
            raise ProgressException(
                    'Insufficient space available to display log messages'
                )
        return True


class LogBoxHandler(Handler):

    def __init__(self, log_box: LogBox):
        self.log_box = log_box
        Handler.__init__(self)

    def emit(self, record):
        self.log_box.add_message(record.getMessage())
        pass


class LogBoxStream():

    def __init__(self, log_box: LogBox):
        self.log_box = log_box

    def write(self, line):
        self.log_box.add_message(line)


class BoxLayout:

    def __init__(self, lines: int, cols: int, padding: int = 1):
        self.lines = lines
        self.cols = cols
        self.padding = padding
        self.current_line = 0
        self._content = []
        self._unpositioned = []
        self.max_row_width = 0

    def add_box(self, box: Box) -> None:
        self._content.append(box)
        self._unpositioned.append(box)

    def add_break(self) -> None:
        self._content.append(None)
        self._unpositioned.append(None)

    def get_layout_properties(self) -> LayoutProperties:
        return LayoutProperties(
                    lines=self.lines,
                    current_line=self.current_line,
                    max_row_width=self.max_row_width
                )

    def _position_row(self, row: list) -> list:
        positioned = []
        extra = []
        row_width = 0
        unpadded_row_width = 0
        row_height = 0
        for box in row:
            box.resize_for_layout(self.get_layout_properties())
            height, width = box.compute_size()
            required_width = width + self.padding
            if len(positioned) and (
                        len(extra) or
                        row_width + required_width > self.cols
                    ):
                extra.append(box)
            else:
                row_width += required_width
                if row_width > self.cols:
                    raise ProgressException('Insufficient columns available')
                unpadded_row_width += width
                row_height = max(row_height, height)
                positioned.append((box, height, width))
        if self.current_line + row_height > self.lines:
            raise ProgressException('Insufficient lines available')
        box_count = len(positioned)
        padding = int((self.cols - unpadded_row_width) / (box_count + 1))
        padded_width = unpadded_row_width + padding * (box_count + 1)
        x = padding + int((self.cols - padded_width) / 2)
        final_row_width = 0
        previous_padding = 0
        for (box, height, width) in positioned:
            final_row_width += previous_padding
            y = self.current_line + round((row_height - height) / 2)
            box.set_position(y, x)
            x += width + padding
            final_row_width += width
            previous_padding = padding
        self.current_line += row_height + self.padding
        self.max_row_width = max(self.max_row_width, final_row_width)
        return extra

    def position(self) -> None:
        row = []
        items = self._unpositioned
        for item in items:
            if item is None:
                row = self._position_row(row)
            else:
                row.append(item)
        while len(row):
            row = self._position_row(row)
        self._unpositioned = []

    def update_content(self) -> None:
        for item in self._content:
            if item is not None:
                item.update()

    def reset(self) -> None:
        self.current_line = 0
        self.max_row_width = 0
        self._unpositioned = self._content.copy()

    def resize(self, lines: int, cols: int) -> None:
        self.lines = lines
        self.cols = cols
        self.reset()
        self.position()


class ProgressDisplay:

    METRICS_PADDING = 1
    METRICS_COUNT = 5
    MIN_MESSAGE_BOX_HEIGHT = 4

    def __init__(self, worker_count: int):
        _displays.append(self)
        self.worker_count = worker_count
        self.results_message = None
        self.pending_resize = False
        self._setup_curses()

    def _setup_curses(self) -> None:
        self.stdscr = curses.initscr()
        curses.noecho()
        curses.curs_set(0)
        self.terminal_size = os.get_terminal_size()
        self._setup_colors()
        self._initialize_content(self.terminal_size)

    def _initialize_content(self, size: os.terminal_size) -> None:
        self.clear()
        self.banner_box = self._initialize_banner()
        self.metric_boxes = self._initialize_metric_boxes()
        self.log_box = self._initialize_log_box()
        self.layout = self._initialize_layout(size)
        self.refresh()

    def _setup_colors(self) -> None:
        curses.start_color()

        curses.init_pair(1, curses.COLOR_CYAN, curses.COLOR_BLACK)
        self.color_brand = curses.color_pair(1)

    def clear(self):
        self.stdscr.clear()

    def refresh(self):
        self.stdscr.noutrefresh()
        curses.doupdate()

    def end_on_input(self):
        curses.flushinp()
        self.stdscr.nodelay(True)
        while True:
            key = self.stdscr.getch()
            if key != -1 and key != curses.KEY_RESIZE:
                break
            if self._resize_if_necessary():
                self._move_cursor_to_log_end()
            sleep(0.1)
        self.end()

    def end(self):
        curses.endwin()
        _displays.remove(self)

    def _initialize_banner(self) -> Optional[BannerBox]:
        banner = get_welcome_banner()
        if banner is None:
            return None

        return BannerBox(
            banner=banner, 
            color=self.color_brand,
            parent=self.stdscr
        )

    def _compute_rate(self, value: int, elapsed_time: float) -> int:
        if elapsed_time > 0:
            return int(value / elapsed_time)
        return 0

    def _get_metrics(
                self,
                update: ScanProgressUpdate,
                worker_index: Optional[int] = None
            ) -> List[Metric]:
        file_count = update.metrics.get_int_metric('counts', worker_index)
        metrics = [Metric('Files Processed', file_count)]
        
        if worker_index is None:
            byte_count = update.metrics.get_int_metric('bytes', worker_index)
            file_rate = self._compute_rate(file_count, update.elapsed_time)
            byte_rate = self._compute_rate(byte_count, update.elapsed_time)
            match_count = update.metrics.get_int_metric('matches', worker_index)

            # Add 'Matches Found' to the top of the list
            metrics = [Metric('Matches Found', match_count)] + metrics

            metrics.extend([
                Metric('Files / Second', file_rate),
                Metric('Bytes Processed', byte_count),
                Metric('Bytes / Second', byte_rate),
            ])

        if len(metrics) > self.METRICS_COUNT:
            raise ValueError("Metrics count is out of sync")

        return metrics

    def _initialize_metric_boxes(self) -> List[MetricBox]:
        default_metrics = ScanMetrics(self.worker_count)
        default_update = ScanProgressUpdate(
                elapsed_time=0,
                metrics=default_metrics
            )
        boxes = []
        for index in range(0, self.worker_count + 1):
            if index == 0:
                worker_index = None
                title = ' Summary '
            else:
                worker_index = index - 1
                title = f' Worker {index} '
            box = MetricBox(
                    self._get_metrics(default_update, worker_index),
                    title=title,
                    parent=self.stdscr
                )
            boxes.append(box)
        return boxes

    def _initialize_log_box(self) -> LogBox:
        log_box = LogBox(
                    # Lines and columns are dynamic
                    columns=10,
                    lines=5,
                    parent=self.stdscr
                )
        return log_box

    def _initialize_layout(self, size: os.terminal_size) -> BoxLayout:
        layout = BoxLayout(size.lines, size.columns, self.METRICS_PADDING)
        if self.banner_box is not None:
            layout.add_box(self.banner_box)
        for index, box in enumerate(self.metric_boxes):
            layout.add_box(box)
            if index == 0:
                layout.add_break()
        layout.add_break()
        layout.add_box(self.log_box)
        layout.position()
        layout.update_content()
        return layout

    def _display_metrics(self, update: ScanProgressUpdate) -> None:
        for index in range(0, self.worker_count + 1):
            box = self.metric_boxes[index]
            worker_index = None if index == 0 else index - 1
            box.metrics = self._get_metrics(update, worker_index)
            box.update()

    def handle_update(self, update: ScanProgressUpdate) -> None:
        self._resize_if_necessary()
        try:
            self._display_metrics(update)
            self.refresh()
        except Exception as e:
            reset_terminal()
            raise ProgressException('Rendering progress update failed') from e

    def queue_resize(self) -> None:
        self.pending_resize = True

    def resize(self) -> None:
        size = os.get_terminal_size()
        smaller = size.columns < self.terminal_size.columns
        self.terminal_size = size
        if smaller:
            self.layout.resize(size.lines, size.columns)
        curses.resizeterm(size.lines, size.columns)
        self.stdscr.erase()
        self.stdscr.refresh()
        self.stdscr.resize(size.lines, size.columns)
        if not smaller:
            self.layout.resize(size.lines, size.columns)
        self.layout.update_content()
        self.refresh()

    def _resize_if_necessary(self) -> bool:
        if not self.pending_resize:
            return False
        try:
            self.resize()
            self.pending_resize = False
            return True
        except Exception as e:
            reset_terminal()
            raise ProgressException(
                    'Failed to adjust progress output to new terminal size'
                ) from e

    def get_log_handler(self) -> logging.Handler:
        return LogBoxHandler(self.log_box)

    def get_output_stream(self) -> LogBoxStream:
        return LogBoxStream(self.log_box)

    def _move_cursor_to_log_end(self) -> None:
        cursor_position = self.log_box.get_cursor_position()
        if cursor_position is not None:
            try:
                self.stdscr.move(cursor_position.y, cursor_position.x + 1)
            except Exception:
                pass

    def scan_finished_handler(
        self, metrics: ScanMetrics, timer: timing.Timer
    ) -> None:
        messages = default_scan_finished_handler(metrics, timer)
        self.results_message = messages.results
        self._move_cursor_to_log_end()
        curses.curs_set(1)

        # Enable the color pair for the success message
        GREEN_TEXT = 3
        BOLD_TEXT = curses.A_BOLD
        curses.init_pair(GREEN_TEXT, curses.COLOR_GREEN, curses.COLOR_BLACK)
        self.stdscr.attron(curses.color_pair(GREEN_TEXT) | BOLD_TEXT)  # Combine color and bold attributes

        # Modify the success message based on whether file paths are present
        success_message = ' Scan completed! Press any key to exit.'
        if self.log_box.has_file_paths:
            success_message = 'Scan completed! Results saved to CSV.'

        # Get the position and width of the "summary" box
        summary_box = self.metric_boxes[0]
        summary_box_position = summary_box.position
        summary_box_width = summary_box.get_width()

        # Calculate the y-coordinate for the success message
        success_y = summary_box_position.y + summary_box.get_height() + 3

        # Calculate the x-coordinate for the success message
        success_x = summary_box_position.x + int((summary_box_width - len(success_message)) / 2)

        # Print the success message in green and bold
        self.stdscr.addstr(success_y, success_x, success_message)

        # Disable the color pair and bold attribute for the success message
        self.stdscr.attroff(curses.color_pair(GREEN_TEXT) | BOLD_TEXT)  # Combine color and bold attributes

        # Set the scan completion flag and update the log box content
        self.log_box.scan_complete = True
        self.log_box.update()