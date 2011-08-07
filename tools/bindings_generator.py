#!/usr/bin/env python

'''
Use CppHeaderParser to parse some C++ headers, and generate binding code for them.

Usage:
        bindings_generator.py BASENAME HEADER1 HEADER2 ... [-- "IGNORED"]

  BASENAME is the name used for output files (with added suffixes).
  HEADER1 etc. are the C++ headers to parse

We generate the following:

  * BASENAME.c: C bindings file, with generated C wrapper functions. You will
                need to build this with your project, and make sure it compiles
                properly by adding the proper #includes etc. You can also just
                #include this file itself in one of your existing project files.

  * BASENAME.js: JavaScript bindings file, with generated JavaScript wrapper
                 objects. This is a high-level wrapping, using native JS classes.

  * IGNORED: Optionally, a list of classes and class::methods not to generate code for.
             Comma separated.

The C bindings file is basically a tiny C wrapper around the C++ code.
It's only purpose is to make it easy to access the C++ code in the JS
bindings, and to prevent DFE from removing the code we care about. The
JS bindings do more serious work, creating class structures in JS and
linking them to the C bindings.
'''

import os, sys, glob, re

abspath = os.path.abspath(os.path.dirname(__file__))
def path_from_root(*pathelems):
  return os.path.join(os.path.sep, *(abspath.split(os.sep)[:-1] + list(pathelems)))
exec(open(path_from_root('tools', 'shared.py'), 'r').read())

# Find ply and CppHeaderParser
sys.path = [path_from_root('third_party', 'ply'), path_from_root('third_party', 'CppHeaderParser')] + sys.path
import CppHeaderParser

#print glob.glob(path_from_root('tests', 'bullet', 'src', 'BulletCollision', 'CollisionDispatch', '*.h'))

basename = sys.argv[1]

ignored = []

if '--' in sys.argv:
  index = sys.argv.index('--')
  ignored = sys.argv[index+1].split(',')
  sys.argv = sys.argv[:index]

# First pass - read everything

classes = {}
struct_parents = {}

text = ''
for header in sys.argv[2:]:
  text += '//// ' + header + '\n'
  text += open(header, 'r').read()
all_h_name = basename + '.all.h'
all_h = open(all_h_name, 'w')
all_h.write(text)
all_h.close()

parsed = CppHeaderParser.CppHeader(all_h_name)
for classname, clazz in parsed.classes.iteritems():
  print 'zz see', classname
  classes[classname] = clazz
  clazz['methods'] = clazz['methods']['public'] # CppHeaderParser doesn't have 'public' etc. in structs. so equalize to that
  for sname, struct in clazz._public_structs.iteritems():
    struct_parents[sname] = classname
    classes[classname + '::' + sname] = struct
    struct['name'] = sname # Missing in CppHeaderParser
    print 'zz seen struct %s in %s' % (sname, classname)

for classname, clazz in classes.iteritems():
  # Various precalculations
  print 'zz precalc', classname
  for method in clazz['methods'][:]:
    method['constructor'] = method['constructor'] or (method['name'] == classname) # work around cppheaderparser issue
    print 'z constructorhmm?', method['name'], method['constructor']#, constructor, method['name'], classname
    args = method['parameters']

    #if method['name'] == 'addWheel': print 'qqqq', classname, method

    # Fill in some missing stuff
    for i in range(len(args)):
      if args[i]['pointer'] and '*' not in args[i]['type']:
        args[i]['type'] += '*'
      if args[i]['reference'] and '&' not in args[i]['type']:
        args[i]['type'] += '&'

    default_param = len(args)+1
    for i in range(len(args)):
      if args[i].get('default'):
        default_param = i+1
        break

    method['num_args'] = set(range(default_param-1, len(args)+1))
    print 'zz ', classname, 'has num_args of', method['num_args']

    if method['static']:
      method['returns'] = method['returns'].replace('static', '')

    # Fill in some missing stuff
    if method.get('returns_const'): method['returns'] = 'const ' + method['returns']
    if method.get('returns_pointer'):
      while method['returns'].count('*') < method['returns_pointer']:
        method['returns'] += '*'
    if method.get('returns_reference'): method['returns'] += '&'

# Explore all functions we need to generate, including parent classes, handling of overloading, etc.

for classname, clazz in parsed.classes.iteritems():
  clazz['final_methods'] = {}

  def explore(subclass):
    # Do our functions first, and do not let later classes override
    for method in subclass['methods']:
      if method['constructor']:
        if clazz != subclass: continue # Subclasses cannot directly use their parent's constructors
      if method['destructor']: continue # Nothing to do there

      if method['name'] not in clazz['final_methods']:
        clazz['final_methods'][method['name']] = {}
        for key in ['name', 'constructor', 'static', 'returns', 'destructor', 'pure_virtual']:
          clazz['final_methods'][method['name']][key] = method[key]
        clazz['final_methods'][method['name']]['num_args'] = method['num_args'].copy()
        clazz['final_methods'][method['name']]['parameters'] = method['parameters'][:]
        clazz['final_methods'][method['name']]['origin'] = subclass
      else:
        # Merge the new function in the best way we can. Shared arguments must match!

        curr = clazz['final_methods'][method['name']]

        if curr['origin'] is not subclass: continue # child class functions mask/hide parent functions of the same name in C++

        if any([curr['parameters'][i]['type'] != method['parameters'][i]['type'] for i in range(min(len(curr['parameters']), len(method['parameters'])))]):
          print 'Warning: Cannot mix in overloaded functions', method['name'], 'in class', classname, ', skipping'
          continue
        # TODO: Other compatibility checks, if any?

        if len(method['parameters']) > len(curr['parameters']):
          curr['parameters'] = method['parameters']

        curr['num_args'] = curr['num_args'].union(method['num_args'])
        print 'zz ', classname, 'has an updated num_args of ', curr['num_args']

    # Recurse
    if subclass.get('inherits'):
      for parent in subclass['inherits']:
        if parent['class'] not in classes:
          print 'Warning: parent class', parent, 'not a known class. Ignoring.'
          return
        explore(classes[parent['class']])

  explore(clazz)

  for method in clazz['final_methods'].itervalues():
    method['num_args'] = list(method['num_args'])
    method['num_args'].sort()

# Second pass - generate bindings
# TODO: Bind virtual functions using dynamic binding in the C binding code

funcs = {} # name -> # of copies in the original, and originalname in a copy
c_funcs = []

gen_c = open(basename + '.cpp', 'w')
gen_js = open(basename + '.js', 'w')

gen_c.write('extern "C" {\n')

def generate_class(generating_classname, classname, clazz): # TODO: deprecate generating?
  generating_classname_head = generating_classname.split('::')[-1]
  classname_head = classname.split('::')[-1]

  inherited = generating_classname_head != classname_head

  for method in clazz['final_methods'].itervalues():
    mname = method['name']
    if classname_head + '::' + mname in ignored: continue

    args = method['parameters']
    constructor = method['constructor']
    destructor = method['destructor']
    static = method['static']

    print "zz generating:", generating_classname, classname, mname, constructor, method['returns']

    if destructor: continue
    if constructor and inherited: continue

    skip = False
    for i in range(len(args)):
      #print 'zz   arggggggg', classname, 'x', mname, 'x', args[i]['name'], 'x', args[i]['type'], 'x', dir(args[i]), 'y', args[i].get('default'), 'z', args[i].get('defaltValue'), args[i].keys()

      if args[i]['name'].replace(' ', '') == '':
        args[i]['name'] = 'arg' + str(i+1)
      elif args[i]['name'] == '&':
        args[i]['name'] = 'arg' + str(i+1)
        args[i]['type'] += '&'

      if '>' in args[i]['name']:
        print 'WARNING: odd ">" in %s, skipping' % classname
        skip = True
        break
      #print 'c1', struct_parents.keys()
      if args[i]['type'][-1] == '&':
        sname = args[i]['type'][:-1]
        if sname[-1] == ' ': sname = sname[:-1]
        if sname in struct_parents:
          args[i]['type'] = struct_parents[sname] + '::' + sname + '&'
        elif sname.replace('const ', '') in struct_parents:
          sname = sname.replace('const ', '')
          args[i]['type'] = 'const ' + struct_parents[sname] + '::' + sname + '&'
      #print 'POST arggggggg', classname, 'x', mname, 'x', args[i]['name'], 'x', args[i]['type']
    if skip:
      continue

    ret = ((classname + ' *') if constructor else method['returns']).replace('virtual ', '')
    callprefix = 'new ' if constructor else ('self->' if not static else (classname + '::'))

    actualmname = ''
    if '__operator__' in mname:
      continue # TODO: operators
    else:
      actualmname = classname if constructor else (method.get('truename') or mname)

    need_self = not constructor and not static
    typedargs = ([] if not need_self else [classname + ' * self']) + map(lambda arg: arg['type'] + ' ' + arg['name'], args)
    justargs = map(lambda arg: arg['name'], args)
    fullname = ('emscripten_bind_' + generating_classname + '__' + mname).replace('::', '__')
    generating_classname_suffixed = generating_classname
    mname_suffixed = mname
    count = funcs.setdefault(fullname, 0)
    funcs[fullname] += 1

    # handle overloading
    dupe = False
    if count > 0:
      dupe = True
      suffix = '_' + str(count+1)
      funcs[fullname + suffix] = 0
      fullname += suffix
      mname_suffixed += suffix
      if constructor:
        generating_classname_suffixed += suffix

    argfixes = '\n'.join(map(lambda arg: '''  %s = (%s && %s.ptr) ? %s.ptr : %s;''' % (arg['name'], arg['name'], arg['name'], arg['name'], arg['name']), args))

    for i in method['num_args']:
      # C

      gen_c.write('''
%s %s_p%d(%s) {
  %s%s%s(%s);
}
''' % (ret, fullname, i, ', '.join(typedargs[:i + (0 if not need_self else 1)]), 'return ' if ret.replace(' ', '') != 'void' else '', callprefix, actualmname, ', '.join(justargs[:i])))

      c_funcs.append(fullname + '_p' + str(i))

    # JS
    calls = ''
    print 'js loopin', method['num_args'], '|', len(args)#, args
    for i in method['num_args']:
      print '    ', i, type(i)
      if i != method['num_args'][0]:
        calls += '  else '
      if i != method['num_args'][-1]:
        calls += '  if (' + justargs[i] + ' === undefined)'
      calls += '\n  ' + ('  ' if len(method['num_args']) > 0 else '')
      if constructor:
        if not dupe:
          calls += '''this.ptr = _%s_p%d(%s);
''' % (fullname, i, ', '.join(justargs[:i]))
        else:
          calls += '''this.ptr = _%s_p%d(%s);
''' % (fullname, i, ', '.join(justargs[:i]))
      else:
        calls += '''%s_%s_p%d(%s);
''' % ('return ' if ret != 'void' else '', fullname, i, ', '.join((['this.ptr'] if need_self else []) + justargs[:i]))

    print 'Maekin:', classname, generating_classname, mname, mname_suffixed
    if constructor:
      if not dupe:
        js_text = '''
function %s(%s) {
%s
%s
}
''' % (mname_suffixed, ', '.join(justargs), argfixes, calls)
      else:
        js_text = '''
function %s(%s) {
%s
%s
}
%s.prototype = %s.prototype;
''' % (mname_suffixed, ', '.join(justargs), argfixes, calls, mname_suffixed, classname)
    else:
      js_text = '''
%s.prototype.%s = function(%s) {
%s
%s
}
''' % (generating_classname, mname_suffixed, ', '.join(justargs), argfixes, calls)

    js_text = js_text.replace('\n\n', '\n').replace('\n\n', '\n')
    gen_js.write(js_text)

# Main loop

for classname, clazz in classes.iteritems():
  if any([name in ignored for name in classname.split('::')]): continue

  # Nothing to generate for pure virtual classes

  def check_pure_virtual(clazz, progeny):
    #if not clazz.get('inherits'): return False # If no inheritance info, not a class, this is a CppHeaderParser struct
    print 'Checking pure virtual for', clazz['name'], clazz['inherits']
    # If we do not recognize any of the parent classes, assume this is pure virtual - ignore it
    if any([((not parent['class'] in classes) or check_pure_virtual(classes[parent['class']], [clazz] + progeny)) for parent in clazz['inherits']]): return True

    def dirtied(mname):
      #print 'zz checking dirtiness for', mname, 'in', progeny
      for progen in progeny:
        for method in progen['methods']:
          if method['name'] == mname and not method['pure_virtual']:
            #print 'zz dirty'
            return True
      #print 'zz not dirtied'
      return False

    for method in clazz['methods']:
      if method['pure_virtual'] and not dirtied(method['name']):
        print 'zz ignoring pure virtual class', classname, 'due to', method['name']
        return True

  if check_pure_virtual(clazz, []):
    continue

  # Add a constructor if none exist
  has_constructor = False
  for method in clazz['methods']:
    mname = method['name']
    has_constructor = has_constructor or (method['constructor'] and not method['destructor'])

  print 'zz', classname, 'has constructor?', has_constructor

  if not has_constructor:
    print 'zz no constructor for', classname, 'so ignoring'
    continue

    clazz['methods'] = [{
      'name': classname,
      'parameters': [],
      'pure_virtual': False,
      'destructor': False,
    }] + clazz['methods']

  generate_class(classname, classname, clazz)

  # TODO: Add a destructor

# Finish up

gen_c.write('''
}

#include <stdio.h>

struct EmscriptenEnsurer
{
  EmscriptenEnsurer() {
    // Actually use the binding functions, so DFE will not eliminate them
    int sum = 0;
    void *seen = (void*)%s;
''' % c_funcs[0])

for func in c_funcs[1:]:
  gen_c.write('''    sum += (void*)%s == seen;
''' % func)

gen_c.write('''    printf("(%d)\\n", sum);
  }
};

EmscriptenEnsurer emscriptenEnsurer;
''')

gen_c.close()
gen_js.close()

