# Copyright 2014 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import absolute_import, print_function

import atexit
import contextlib
import errno
import os
import shutil
import stat
import sys
import tempfile
import threading
import time
import zipfile
from collections import defaultdict
from datetime import datetime
from uuid import uuid4

# We use the start of MS-DOS time, which is what zipfiles use (see section 4.4.6 of
# https://pkware.cachefly.net/webdocs/casestudies/APPNOTE.TXT).
DETERMINISTIC_DATETIME = datetime(
  year=1980, month=1, day=1, hour=0, minute=0, second=0, tzinfo=None
)


def die(msg, exit_code=1):
  print(msg, file=sys.stderr)
  sys.exit(exit_code)


def safe_copy(source, dest, overwrite=False):
  def do_copy():
    temp_dest = dest + uuid4().hex
    shutil.copy(source, temp_dest)
    os.rename(temp_dest, dest)

  # If the platform supports hard-linking, use that and fall back to copying.
  # Windows does not support hard-linking.
  if hasattr(os, 'link'):
    try:
      os.link(source, dest)
    except OSError as e:
      if e.errno == errno.EEXIST:
        # File already exists.  If overwrite=True, write otherwise skip.
        if overwrite:
          do_copy()
      elif e.errno == errno.EXDEV:
        # Hard link across devices, fall back on copying
        do_copy()
      else:
        raise
  elif os.path.exists(dest):
    if overwrite:
      do_copy()
  else:
    do_copy()


# See http://stackoverflow.com/questions/2572172/referencing-other-modules-in-atexit
class MktempTeardownRegistry(object):
  def __init__(self):
    self._registry = defaultdict(set)
    self._getpid = os.getpid
    self._lock = threading.RLock()
    self._exists = os.path.exists
    self._getenv = os.getenv
    self._rmtree = shutil.rmtree
    atexit.register(self.teardown)

  def __del__(self):
    self.teardown()

  def register(self, path):
    with self._lock:
      self._registry[self._getpid()].add(path)
    return path

  def teardown(self):
    for td in self._registry.pop(self._getpid(), []):
      if self._exists(td):
        self._rmtree(td)


_MKDTEMP_SINGLETON = MktempTeardownRegistry()


class PermPreservingZipFile(zipfile.ZipFile, object):
  """A ZipFile that works around https://bugs.python.org/issue15795"""

  @classmethod
  def zip_info_from_file(cls, filename, arcname=None, date_time=None):
    """Construct a ZipInfo for a file on the filesystem.

    Usually this is provided directly as a method of ZipInfo, but it is not implemented in Python
    2.7 so we re-implement it here. The main divergance we make from the original is adding a
    parameter for the datetime (a time.struct_time), which allows us to use a deterministic
    timestamp. See https://github.com/python/cpython/blob/master/Lib/zipfile.py#L495."""
    st = os.stat(filename)
    isdir = stat.S_ISDIR(st.st_mode)
    if arcname is None:
      arcname = filename
    arcname = os.path.normpath(os.path.splitdrive(arcname)[1])
    while arcname[0] in (os.sep, os.altsep):
      arcname = arcname[1:]
    if isdir:
      arcname += '/'
    if date_time is None:
      date_time = time.localtime(st.st_mtime)
    zinfo = zipfile.ZipInfo(filename=arcname, date_time=date_time[:6])
    zinfo.external_attr = (st.st_mode & 0xFFFF) << 16  # Unix attributes
    if isdir:
      zinfo.file_size = 0
      zinfo.external_attr |= 0x10  # MS-DOS directory flag
    else:
      zinfo.file_size = st.st_size
    return zinfo

  def _extract_member(self, member, targetpath, pwd):
    result = super(PermPreservingZipFile, self)._extract_member(member, targetpath, pwd)
    info = member if isinstance(member, zipfile.ZipInfo) else self.getinfo(member)
    self._chmod(info, result)
    return result

  def _chmod(self, info, path):
    # This magic works to extract perm bits from the 32 bit external file attributes field for
    # unix-created zip files, for the layout, see:
    #   https://www.forensicswiki.org/wiki/ZIP#External_file_attributes
    attr = info.external_attr >> 16
    os.chmod(path, attr)


@contextlib.contextmanager
def open_zip(path, *args, **kwargs):
  """A contextmanager for zip files. Passes through positional and kwargs to zipfile.ZipFile."""
  with contextlib.closing(PermPreservingZipFile(path, *args, **kwargs)) as zip:
    yield zip


def safe_mkdtemp(**kw):
  """Create a temporary directory that is cleaned up on process exit.

  Takes the same parameters as tempfile.mkdtemp.
  """
  # proper lock sanitation on fork [issue 6721] would be desirable here.
  return _MKDTEMP_SINGLETON.register(tempfile.mkdtemp(**kw))


def register_rmtree(directory):
  """Register an existing directory to be cleaned up at process exit."""
  return _MKDTEMP_SINGLETON.register(directory)


def safe_mkdir(directory, clean=False):
  """Safely create a directory.

  Ensures a directory is present.  If it's not there, it is created.  If it
  is, it's a no-op. If clean is True, ensures the directory is empty.
  """
  if clean:
    safe_rmtree(directory)
  try:
    os.makedirs(directory)
  except OSError as e:
    if e.errno != errno.EEXIST:
      raise


def safe_open(filename, *args, **kwargs):
  """Safely open a file.

  ``safe_open`` ensures that the directory components leading up the
  specified file have been created first.
  """
  filepath = safe_path(filename)
  safe_mkdir(os.path.dirname(filepath))
  return open(filename, *args, **kwargs)  # noqa: T802


def safe_delete(filename):
  """Delete a file safely. If it's not present, no-op."""
  try:
    os.unlink(filename)
  except OSError as e:
    if e.errno != errno.ENOENT:
      raise


def safe_rmtree(directory):
  """Delete a directory if it's present. If it's not present, no-op."""
  if os.path.exists(directory):
    shutil.rmtree(directory, True)


def safe_sleep(seconds):
  """Ensure that the thread sleeps at a minimum the requested seconds.

  Until Python 3.5, there was no guarantee that time.sleep() would actually sleep the requested
  time. See https://docs.python.org/3/library/time.html#time.sleep."""
  if sys.version_info[0:2] >= (3, 5):
    time.sleep(seconds)
  else:
    start_time = current_time = time.time()
    while current_time - start_time < seconds:
      remaining_time = seconds - (current_time - start_time)
      time.sleep(remaining_time)
      current_time = time.time()


def safe_path(path):
  """Make a path safe for Windows.
  Windows has a max path length of 260 character by default, which can be
  expanded to 32767 characters by appending a "\\?\" prefix:
  https://docs.microsoft.com/windows/desktop/FileIO/naming-a-file#maximum-path-length-limitation
  If running Windows, add extended-path prefix if it's not already present. If not running
  Windows, path is not modified.
  """
  if os.name == "nt":
    extended_path_prefix = u"\\\\?\\"
    if not path.startswith(extended_path_prefix):
      return os.path.abspath(extended_path_prefix + path)
  return path


def rename_if_empty(src, dest, allowable_errors=(errno.EEXIST, errno.ENOTEMPTY)):
  """Rename `src` to `dest` using `os.rename()`.

  If an `OSError` with errno in `allowable_errors` is encountered during the rename, the `dest`
  dir is left unchanged and the `src` directory will simply be removed.
  """
  try:
    os.rename(src, dest)
  except OSError as e:
    if e.errno in allowable_errors:
      safe_rmtree(src)
    else:
      raise


def chmod_plus_x(path):
  """Equivalent of unix `chmod a+x path`"""
  path_mode = os.stat(path).st_mode
  path_mode &= int('777', 8)
  if path_mode & stat.S_IRUSR:
    path_mode |= stat.S_IXUSR
  if path_mode & stat.S_IRGRP:
    path_mode |= stat.S_IXGRP
  if path_mode & stat.S_IROTH:
    path_mode |= stat.S_IXOTH
  os.chmod(path, path_mode)


def chmod_plus_w(path):
  """Equivalent of unix `chmod +w path`"""
  path_mode = os.stat(path).st_mode
  path_mode &= int('777', 8)
  path_mode |= stat.S_IWRITE
  os.chmod(path, path_mode)


def touch(file, times=None):
  """Equivalent of unix `touch path`.

  :file The file to touch.
  :times Either a tuple of (atime, mtime) or else a single time to use for both.  If not
  specified both atime and mtime are updated to the current time.
  """
  if times:
    if len(times) > 2:
      raise ValueError('times must either be a tuple of (atime, mtime) or else a single time value '
                       'to use for both.')

    if len(times) == 1:
      times = (times, times)

  with safe_open(file, 'a'):
    os.utime(file, times)


class Chroot(object):
  """A chroot of files overlayed from one directory to another directory.

  Files may be tagged when added in order to keep track of multiple overlays
  in the chroot.
  """
  class Error(Exception): pass
  class ChrootTaggingException(Error):
    def __init__(self, filename, orig_tag, new_tag):
      super(Chroot.ChrootTaggingException, self).__init__(  # noqa: T800
        "Trying to add %s to fileset(%s) but already in fileset(%s)!" % (
          filename, new_tag, orig_tag))

  def __init__(self, chroot_base):
    """Create the chroot.

    :chroot_base Directory for the creation of the target chroot.
    """
    try:
      safe_mkdir(chroot_base)
    except OSError as e:
      raise self.ChrootException('Unable to create chroot in %s: %s' % (chroot_base, e))
    self.chroot = chroot_base
    self.filesets = defaultdict(set)

  def clone(self, into=None):
    """Clone this chroot.

    :keyword into: (optional) An optional destination directory to clone the
      Chroot into.  If not specified, a temporary directory will be created.

    .. versionchanged:: 0.8
      The temporary directory created when ``into`` is not specified is now garbage collected on
      interpreter exit.
    """
    into = into or safe_mkdtemp()
    new_chroot = Chroot(into)
    for label, fileset in self.filesets.items():
      for fn in fileset:
        new_chroot.link(os.path.join(self.chroot, fn), fn, label=label)
    return new_chroot

  def path(self):
    """The path of the chroot."""
    return self.chroot

  def _normalize(self, dst):
    dst = os.path.normpath(dst)
    if dst.startswith(os.sep) or dst.startswith('..'):
      raise self.Error('Destination path is not a relative path!')
    return dst

  def _check_tag(self, fn, label):
    for fs_label, fs in self.filesets.items():
      if fn in fs and fs_label != label:
        raise self.ChrootTaggingException(fn, fs_label, label)

  def _tag(self, fn, label):
    self._check_tag(fn, label)
    self.filesets[label].add(fn)

  def _ensure_parent(self, path):
    safe_mkdir(os.path.dirname(os.path.join(self.chroot, path)))

  def copy(self, src, dst, label=None):
    """Copy file ``src`` to ``chroot/dst`` with optional label.

    May raise anything shutil.copy can raise, e.g.
      IOError(Errno 21 'EISDIR')

    May raise ChrootTaggingException if dst is already in a fileset
    but with a different label.
    """
    dst = self._normalize(dst)
    self._tag(dst, label)
    self._ensure_parent(dst)
    shutil.copy(src, os.path.join(self.chroot, dst))

  def link(self, src, dst, label=None):
    """Hard link file from ``src`` to ``chroot/dst`` with optional label.

    May raise anything os.link can raise, e.g.
      IOError(Errno 21 'EISDIR')

    May raise ChrootTaggingException if dst is already in a fileset
    but with a different label.
    """
    dst = self._normalize(dst)
    self._tag(dst, label)
    self._ensure_parent(dst)
    abs_src = src
    abs_dst = os.path.join(self.chroot, dst)
    safe_copy(abs_src, abs_dst, overwrite=False)
    # TODO: Ensure the target and dest are the same if the file already exists.

  def write(self, data, dst, label=None, mode='wb'):
    """Write data to ``chroot/dst`` with optional label.

    Has similar exceptional cases as ``Chroot.copy``
    """
    dst = self._normalize(dst)
    self._tag(dst, label)
    self._ensure_parent(dst)
    with open(os.path.join(self.chroot, dst), mode) as wp:
      wp.write(data)

  def touch(self, dst, label=None):
    """Perform 'touch' on ``chroot/dst`` with optional label.

    Has similar exceptional cases as Chroot.copy
    """
    dst = self._normalize(dst)
    self._tag(dst, label)
    touch(os.path.join(self.chroot, dst))

  def get(self, label):
    """Get all files labeled with ``label``"""
    return self.filesets.get(label, set())

  def files(self):
    """Get all files in the chroot."""
    all_files = set()
    for label in self.filesets:
      all_files.update(self.filesets[label])
    return all_files

  def labels(self):
    return self.filesets.keys()

  def __str__(self):
    return 'Chroot(%s {fs:%s})' % (self.chroot,
      ' '.join('%s' % foo for foo in self.filesets.keys()))

  def delete(self):
    shutil.rmtree(self.chroot)

  def zip(self, filename, mode='w', deterministic_timestamp=False):
    with open_zip(filename, mode) as zf:
      for f in sorted(self.files()):
        full_path = os.path.join(self.chroot, f)
        zinfo = zf.zip_info_from_file(
            filename=full_path,
            arcname=f,
            date_time=DETERMINISTIC_DATETIME.timetuple() if deterministic_timestamp else None
        )
        with open(full_path, 'rb') as open_f:
          data = open_f.read()
        zf.writestr(zinfo, data, compress_type=zipfile.ZIP_DEFLATED)
