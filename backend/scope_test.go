package main

import (
	"reflect"
	"testing"
)

func TestNormalizeScope(t *testing.T) {
	for _, tc := range []struct {
		name, single string
		input, want  []string
		invalid      bool
	}{
		{name: "default", want: []string{}},
		{name: "legacy", single: "a", want: []string{"a"}},
		{name: "multiple", input: []string{"b", "a", "b"}, want: []string{"a", "b"}},
		{name: "ambiguous", single: "a", input: []string{"b"}, invalid: true},
		{name: "empty ID", input: []string{" "}, invalid: true},
		{name: "too many", input: make([]string, 33), invalid: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got, err := normalizeScope(tc.single, tc.input)
			if (err != nil) != tc.invalid {
				t.Fatalf("error=%v", err)
			}
			if !tc.invalid && !reflect.DeepEqual(got, tc.want) {
				t.Fatalf("got %v, want %v", got, tc.want)
			}
		})
	}
}
