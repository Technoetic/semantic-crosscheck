"""Private pySHACL subprocess entry point; only synthetic Turtle is accepted."""
import sys


def main():
    try:
        import rdflib
        from pyshacl import validate
        data = rdflib.Graph().parse(sys.argv[1], format='turtle')
        shapes = rdflib.Graph().parse(sys.argv[2], format='turtle')
        _conforms, report, _text = validate(data, shacl_graph=shapes, inference='none',
                                          advanced=False, abort_on_first=False)
        if not isinstance(report, rdflib.Graph):
            return 1
        sys.stdout.write(report.serialize(format='turtle'))
        return 0
    except Exception:
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
