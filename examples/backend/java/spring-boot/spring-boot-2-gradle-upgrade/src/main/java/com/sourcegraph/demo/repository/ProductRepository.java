package com.sourcegraph.demo.repository;

import com.sourcegraph.demo.entity.Product;
import org.springframework.data.jpa.repository.JpaRepository;

public interface ProductRepository extends JpaRepository<Product, Long> {
}
