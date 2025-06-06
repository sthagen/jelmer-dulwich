All functionality should be available in pure Python. Optional Rust
implementations may be written for performance reasons, but should never
replace the Python implementation.

Where possible include updates to NEWS along with your improvements.

New functionality and bug fixes should be accompanied by matching unit tests.

Coding style
------------
Where possible, please follow PEP8 with regard to coding style. Run ruff.

Furthermore, triple-quotes should always be """, single quotes are ' unless
using " would result in less escaping within the string.

Public methods, functions and classes should all have doc strings. Please use
Google style docstrings to document parameters and return values.
You can generate the documentation by running "make doc".

Running the tests
-----------------
To run the testsuite, you should be able to simply run "make check". This
will run the tests using unittest.

::
   $ make check

Tox configuration is also present.

String Types
------------
Like Linux, Git treats filenames as arbitrary bytestrings. There is no prescribed
encoding for these strings, and although it is fairly common to use UTF-8, any
raw byte strings are supported.

For this reason, the lower levels in Dulwich treat git-based filenames as
bytestrings. It is up to the Dulwich API user to encode and decode them if
necessary. The porcelain may accept unicode strings and convert them to
bytestrings as necessary on the fly (using 'utf-8').

* on-disk filenames: regular strings, or ideally, pathlib.Path instances
* git-repository related filenames: bytes
* object sha1 digests (20 bytes long): bytes
* object sha1 hexdigests (40 bytes long): str (bytestrings on python2, strings
  on python3)

Merge requests
--------------
Please either send pull requests to the maintainer (jelmer@jelmer.uk) or create
new pull requests on GitHub.

Deprecating functionality
-------------------------
Dulwich uses the `dissolve` package to manage deprecations. If you want to deprecate
functionality, please use the `@replace_me` decorator from the root of the
dulwich package. This will ensure that the deprecation is handled correctly:

* It will be logged as a warning
* When the version of Dulwich is bumped, the deprecation will be removed
* Users can use `dissolve migrate` to automatically replace deprecated
  functionality in their code

Licensing
---------
All contributions should be made under the same license that Dulwich itself
comes under: both Apache License, version 2.0 or later and GNU General Public
License, version 2.0 or later.
