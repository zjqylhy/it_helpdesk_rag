package main

import (
	"errors"
	"sort"
	"strings"
)

// 统一旧单库与新多库入口，空集合表示默认业务范围。
func normalizeScope(single string, selected []string) ([]string, error) {
	if single != "" && len(selected) > 0 {
		return nil, errors.New("不能同时指定 kb_id 和 kb_ids")
	}
	if single != "" {
		selected = []string{single}
	}
	if len(selected) > 32 {
		return nil, errors.New("最多选择 32 个知识库")
	}
	result := []string{}
	seen := map[string]bool{}
	for _, value := range selected {
		if strings.TrimSpace(value) == "" || len(value) > 128 {
			return nil, errors.New("知识库标识无效")
		}
		if !seen[value] {
			seen[value] = true
			result = append(result, value)
		}
	}
	sort.Strings(result)
	return result, nil
}
